from datetime import timedelta
from decimal import Decimal

from django.utils import timezone
from rest_framework.exceptions import APIException

from core.models import (
    Subscription,
    SubscriptionDiscount,
    SubscriptionPayment,
)
from core.services.tenants import (
    TenantLifecycleService,
    _BILLING_CYCLE_DAYS,
    _BILLING_CYCLE_PRICE_FIELD,
)


class PaymentAlreadyConfirmedError(APIException):
    status_code = 409
    default_code = "PAYMENT_ALREADY_CONFIRMED"
    default_detail = {
        "error": {
            "code": "PAYMENT_ALREADY_CONFIRMED",
            "message": "Este pago ya fue confirmado.",
        }
    }


class SubscriptionPaymentService:
    """Confirmacion manual de un pago recibido por transferencia bancaria
    directa (Especificacion de API §4.10; TRD §6.3, sin pasarela de pago).
    Confirmar extiende la vigencia de la suscripcion desde su vencimiento
    actual (o desde ahora, si ya estaba vencida) segun su billing_cycle, y
    la reactiva si estaba past_due."""

    @staticmethod
    def confirm_payment(payment: SubscriptionPayment) -> SubscriptionPayment:
        if payment.status == "PAID":
            raise PaymentAlreadyConfirmedError()

        payment.status = "PAID"
        payment.paid_at = timezone.now()
        payment.save(update_fields=["status", "paid_at"])

        subscription = payment.subscription
        extension_days = _BILLING_CYCLE_DAYS[subscription.billing_cycle]
        base = max(subscription.expires_at, timezone.now())
        subscription.expires_at = base + timedelta(days=extension_days)
        if subscription.status == "past_due":
            subscription.status = "active"

        update_fields = ["expires_at", "status"]
        # Sprint 11: si el tenant tiene un descuento vigente, se refleja en
        # el precio real cobrado a partir de ESTA confirmacion (no cambia
        # pagos ya confirmados en el pasado).
        discount = SubscriptionDiscountService.get_active_discount(subscription)
        if discount is not None:
            subscription.price_paid = SubscriptionDiscountService.apply_discount(
                subscription, discount
            )
            update_fields.append("price_paid")

        subscription.save(update_fields=update_fields)

        # Sprint 35: un tenant suspended por falta de pago no volvia a
        # active solo -antes de esto, confirmar el pago (que si reactivaba
        # la Subscription) dejaba al tenant bloqueado indefinidamente hasta
        # que alguien de platform_staff recordara ir a reactivarlo a mano
        # en una pantalla completamente separada. El ciclo de negocio
        # "vencimiento -> suspension -> pago -> reactivacion" (Especificacion
        # de API §4.12) exige que el pago SI la restaure.
        if subscription.tenant.status == "suspended":
            TenantLifecycleService.reactivate_tenant(subscription.tenant)

        return payment


class InvalidDiscountError(APIException):
    status_code = 400
    default_code = "INVALID_DISCOUNT"
    default_detail = {
        "error": {
            "code": "INVALID_DISCOUNT",
            "message": "Especifique discount_percent o override_price, nunca ambos.",
        }
    }


class SubscriptionDiscountService:
    """Condicion especial de precio negociada con un tenant puntual
    (Especificacion de API §4.25) -se aplica recien en la SIGUIENTE
    confirmacion de pago (SubscriptionPaymentService.confirm_payment), nunca
    retroactivamente sobre pagos ya confirmados."""

    @staticmethod
    def create_discount(
        *,
        subscription: Subscription,
        discount_percent=None,
        override_price=None,
        reason: str,
        expires_at=None,
    ) -> SubscriptionDiscount:
        has_percent = discount_percent is not None
        has_override = override_price is not None
        if has_percent == has_override:  # ambos o ninguno
            raise InvalidDiscountError()
        return SubscriptionDiscount.objects.create(
            subscription=subscription,
            discount_percent=discount_percent,
            override_price=override_price,
            reason=reason,
            expires_at=expires_at,
        )

    @staticmethod
    def get_active_discount(subscription: Subscription) -> SubscriptionDiscount | None:
        from django.db.models import Q

        now = timezone.now()
        return (
            SubscriptionDiscount.objects.filter(subscription=subscription)
            .filter(Q(expires_at__isnull=True) | Q(expires_at__gt=now))
            .order_by("-created_at")
            .first()
        )

    @staticmethod
    def apply_discount(subscription: Subscription, discount: SubscriptionDiscount):

        if discount.override_price is not None:
            return discount.override_price

        plan_price = getattr(
            subscription.plan, _BILLING_CYCLE_PRICE_FIELD[subscription.billing_cycle]
        )
        return plan_price * (Decimal("1") - discount.discount_percent / Decimal("100"))

    @staticmethod
    def remove_discount(discount: SubscriptionDiscount) -> None:
        discount.delete()
