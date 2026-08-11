import calendar
import io
from datetime import date, timedelta

import qrcode
from django.db import transaction
from rest_framework.exceptions import APIException

from gimnasio.models import ClassBooking, Membership, MembershipGroup, MembershipPayment


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


class ClassFullError(APIException):
    status_code = 409
    default_code = "CLASS_FULL"
    default_detail = {
        "error": {
            "code": "CLASS_FULL",
            "message": "No hay cupo disponible para esta clase en la fecha indicada.",
        }
    }


class ClassBookingAlreadyCancelledError(APIException):
    status_code = 409
    default_code = "CLASS_BOOKING_ALREADY_CANCELLED"
    default_detail = {
        "error": {
            "code": "CLASS_BOOKING_ALREADY_CANCELLED",
            "message": "Esta reserva ya esta cancelada.",
        }
    }


class ClassBookingNotReservedError(APIException):
    status_code = 409
    default_code = "CLASS_BOOKING_NOT_RESERVED"
    default_detail = {
        "error": {
            "code": "CLASS_BOOKING_NOT_RESERVED",
            "message": "Solo se puede marcar asistencia sobre una reserva vigente.",
        }
    }


class MembershipGroupSizeError(APIException):
    status_code = 400
    default_code = "MEMBERSHIP_GROUP_TOO_SMALL"
    default_detail = {
        "error": {
            "code": "MEMBERSHIP_GROUP_TOO_SMALL",
            "message": "Un grupo familiar/grupal necesita al menos 2 membresias.",
        }
    }


class ClassBookingService:
    """El cupo se cuenta por GymClass+fecha, no por ClassSchedule -una
    misma clase puede repetirse en varios horarios de la semana, pero lo
    que limita el cupo es cuantos socios ya reservaron ese dia puntual
    (Sprint 30, Ficha de Producto §5.1)."""

    @staticmethod
    @transaction.atomic
    def book_class(*, customer, gym_class, class_date: date) -> ClassBooking:
        taken = ClassBooking.objects.filter(
            gym_class=gym_class,
            class_date=class_date,
            status__in=["RESERVADO", "ASISTIO"],
        ).count()
        if taken >= gym_class.max_capacity:
            raise ClassFullError()
        return ClassBooking.objects.create(
            customer=customer,
            gym_class=gym_class,
            class_date=class_date,
            status="RESERVADO",
        )

    @staticmethod
    def mark_attendance(*, booking: ClassBooking, attended: bool) -> ClassBooking:
        if booking.status != "RESERVADO":
            raise ClassBookingNotReservedError()
        booking.status = "ASISTIO" if attended else "NO_ASISTIO"
        booking.save(update_fields=["status"])
        return booking

    @staticmethod
    def cancel_booking(*, booking: ClassBooking) -> ClassBooking:
        if booking.status == "CANCELADO":
            raise ClassBookingAlreadyCancelledError()
        booking.status = "CANCELADO"
        booking.save(update_fields=["status"])
        return booking


class MembershipGroupService:
    """Une 2+ Membership bajo un solo MembershipGroup con un titular de
    pago (Sprint 30) -las membresias mantienen su propio ciclo de vida
    (renovar/congelar/cancelar via MembershipService), el grupo solo
    las agrupa para cobro."""

    @staticmethod
    @transaction.atomic
    def create_group(
        *, holder_customer, name: str, memberships: list[Membership]
    ) -> MembershipGroup:
        if len(memberships) < 2:
            raise MembershipGroupSizeError()
        group = MembershipGroup.objects.create(
            holder_customer=holder_customer, name=name
        )
        for membership in memberships:
            membership.group = group
            membership.save(update_fields=["group"])
        return group


class InvalidMembershipQrTokenError(APIException):
    status_code = 400
    default_code = "INVALID_MEMBERSHIP_QR_TOKEN"
    default_detail = {
        "error": {
            "code": "INVALID_MEMBERSHIP_QR_TOKEN",
            "message": "El codigo QR no corresponde a una membresia valida.",
        }
    }


_QR_TOKEN_PREFIX = "FIVUZA-MEMBERSHIP-"

_DENIAL_REASONS = {
    "FROZEN": "MEMBERSHIP_FROZEN",
    "EXPIRED": "MEMBERSHIP_EXPIRED",
    "CANCELLED": "MEMBERSHIP_CANCELLED",
}


class AccessCheckService:
    """Control de acceso del socio (Sprint 31, Ficha de Producto §5.1):
    deliberadamente NO se acopla a una marca de torniquete o lector QR
    especifica -expone un endpoint de verificacion simple (permitido/
    denegado + motivo) para que cualquier hardware de terreno lo consulte,
    y una credencial QR generica (Sprint 27 ya sento el mismo patron con
    codigos de barras: el backend genera la imagen, el consumidor final
    -impresora fisica o, aqui, un lector QR- es libre)."""

    @staticmethod
    def check_access(membership: Membership, *, at: date | None = None) -> dict:
        at = at or date.today()
        if membership.status == "ACTIVE" and membership.end_date >= at:
            return {"allowed": True, "reason": None}
        if membership.status == "ACTIVE" and membership.end_date < at:
            # Todavia no paso por expire_overdue_memberships() (corre cada
            # 15 min), pero para efectos de acceso ya esta vencida.
            return {"allowed": False, "reason": "MEMBERSHIP_EXPIRED"}
        return {
            "allowed": False,
            "reason": _DENIAL_REASONS.get(membership.status, membership.status),
        }

    @staticmethod
    def qr_token(membership: Membership) -> str:
        return f"{_QR_TOKEN_PREFIX}{membership.id}"

    @staticmethod
    def parse_qr_token(token: str) -> int:
        if not token.startswith(_QR_TOKEN_PREFIX):
            raise InvalidMembershipQrTokenError()
        try:
            return int(token[len(_QR_TOKEN_PREFIX) :])
        except ValueError as exc:
            raise InvalidMembershipQrTokenError() from exc

    @staticmethod
    def generate_qr_png(membership: Membership) -> bytes:
        image = qrcode.make(AccessCheckService.qr_token(membership))
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return buffer.getvalue()
