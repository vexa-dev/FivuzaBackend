import calendar
from datetime import date, timedelta

from django.db import transaction
from rest_framework.exceptions import APIException

from gimnasio.models import Membership, MembershipPayment


def _add_months(base: date, months: int) -> date:
    month_index = base.month - 1 + months
    year = base.year + month_index // 12
    month = month_index % 12 + 1
    day = min(base.day, calendar.monthrange(year, month)[1])
    return base.replace(year=year, month=month, day=day)


def _add_period(base: date, periodicity: str) -> date:
    if periodicity == "MONTHLY":
        return _add_months(base, 1)
    if periodicity == "QUARTERLY":
        return _add_months(base, 3)
    return _add_months(base, 12)  # YEARLY


class MembershipNotActiveError(APIException):
    status_code = 409
    default_code = "MEMBERSHIP_NOT_ACTIVE"
    default_detail = {
        "error": {
            "code": "MEMBERSHIP_NOT_ACTIVE",
            "message": "Esta membresia no esta activa.",
        }
    }


class MembershipNotFrozenError(APIException):
    status_code = 409
    default_code = "MEMBERSHIP_NOT_FROZEN"
    default_detail = {
        "error": {
            "code": "MEMBERSHIP_NOT_FROZEN",
            "message": "Esta membresia no esta congelada.",
        }
    }


class MembershipNotRenewableError(APIException):
    status_code = 409
    default_code = "MEMBERSHIP_NOT_RENEWABLE"
    default_detail = {
        "error": {
            "code": "MEMBERSHIP_NOT_RENEWABLE",
            "message": "Solo se puede renovar una membresia activa o vencida.",
        }
    }


class MembershipAlreadyCancelledError(APIException):
    status_code = 409
    default_code = "MEMBERSHIP_ALREADY_CANCELLED"
    default_detail = {
        "error": {
            "code": "MEMBERSHIP_ALREADY_CANCELLED",
            "message": "Esta membresia ya esta cancelada.",
        }
    }


class MembershipService:
    """Unico punto de entrada para el ciclo de vida de una Membership
    (Sprint 29, Ficha de Producto §5.1). end_date se calcula sumando la
    periodicidad del plan sobre una fecha base -nunca a mano desde el
    caller, para que crear/renovar siempre midan el periodo igual."""

    @staticmethod
    @transaction.atomic
    def create_membership(*, customer, plan, start_date: date, user) -> Membership:
        end_date = _add_period(start_date, plan.periodicity)
        return Membership.objects.create(
            customer=customer,
            plan=plan,
            start_date=start_date,
            end_date=end_date,
            status="ACTIVE",
        )

    @staticmethod
    @transaction.atomic
    def renew_membership(
        *,
        membership: Membership,
        user,
        payment_amount=None,
        payment_method: str = "CASH",
    ) -> Membership:
        if membership.status not in ("ACTIVE", "EXPIRED"):
            raise MembershipNotRenewableError()

        # Si todavia no vencio, la renovacion se suma sobre su propio
        # end_date (no se "pierde" el tiempo que le queda); si ya vencio,
        # se suma desde hoy.
        base = (
            membership.end_date if membership.end_date >= date.today() else date.today()
        )
        membership.end_date = _add_period(base, membership.plan.periodicity)
        membership.status = "ACTIVE"
        membership.save(update_fields=["end_date", "status"])

        if payment_amount is not None:
            MembershipPayment.objects.create(
                membership=membership,
                amount=payment_amount,
                method=payment_method,
                user=user,
            )
        return membership

    @staticmethod
    def freeze_membership(*, membership: Membership, user) -> Membership:
        if membership.status != "ACTIVE":
            raise MembershipNotActiveError()
        membership.status = "FROZEN"
        membership.frozen_since = date.today()
        membership.save(update_fields=["status", "frozen_since"])
        return membership

    @staticmethod
    def unfreeze_membership(*, membership: Membership, user) -> Membership:
        if membership.status != "FROZEN":
            raise MembershipNotFrozenError()
        frozen_days = (date.today() - membership.frozen_since).days
        membership.end_date = membership.end_date + timedelta(days=frozen_days)
        membership.status = "ACTIVE"
        membership.frozen_since = None
        membership.save(update_fields=["end_date", "status", "frozen_since"])
        return membership

    @staticmethod
    def cancel_membership(*, membership: Membership, user) -> Membership:
        if membership.status == "CANCELLED":
            raise MembershipAlreadyCancelledError()
        membership.status = "CANCELLED"
        membership.save(update_fields=["status"])
        return membership

    @staticmethod
    def expire_overdue_memberships(*, at: date | None = None) -> int:
        """Usado por la tarea periodica de Celery (gimnasio.tasks) -una
        membresia FROZEN nunca vence mientras esta congelada, solo se
        filtran las ACTIVE."""
        at = at or date.today()
        return Membership.objects.filter(status="ACTIVE", end_date__lt=at).update(
            status="EXPIRED"
        )

    @staticmethod
    def get_expiring_soon(*, days: int = 7, at: date | None = None):
        at = at or date.today()
        threshold = at + timedelta(days=days)
        return (
            Membership.objects.filter(
                status="ACTIVE", end_date__gte=at, end_date__lte=threshold
            )
            .select_related("customer", "plan")
            .order_by("end_date")
        )
