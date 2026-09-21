from decimal import Decimal

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone
from rest_framework.exceptions import APIException

from inventario.models import Stock
from ventas.models import (
    CashSession,
    ProductReservation,
    Sale,
)
from ventas.services.sales import InsufficientStockError, SaleService


class ReservationNotActiveError(APIException):
    status_code = 409
    default_code = "RESERVATION_NOT_ACTIVE"
    default_detail = {
        "error": {
            "code": "RESERVATION_NOT_ACTIVE",
            "message": "Esta reserva ya no esta activa.",
        }
    }


class ReservationService:
    """Apartado de stock para un cliente sin registrar la venta todavia
    (Sprint 28, Ficha de Producto §5.2). No genera InventoryMovement al
    crear la reserva -el stock fisico se descuenta recien al convertir()
    (via SaleService.create_sale(), el mismo camino de cualquier venta). La
    disponibilidad "vendible" resta las reservas ACTIVE del Stock real, asi
    que dos reservas y una venta nunca pueden sobre-comprometer el mismo
    stock fisico."""

    @staticmethod
    def get_available_quantity(*, variant, warehouse) -> Decimal:
        stock = Stock.objects.filter(variant=variant, warehouse=warehouse).first()
        current = stock.quantity if stock else Decimal("0")
        reserved = ProductReservation.objects.filter(
            variant=variant, warehouse=warehouse, status="ACTIVE"
        ).aggregate(total=Sum("quantity"))["total"] or Decimal("0")
        return current - reserved

    @staticmethod
    @transaction.atomic
    def create_reservation(
        *, customer, variant, warehouse, quantity: Decimal, expires_at, user
    ) -> ProductReservation:
        # select_for_update sobre la fila de Stock serializa esto con
        # cualquier venta/traslado/otra reserva concurrente de la misma
        # variante+almacen -mismo patron que StockService.adjust_stock()
        # (Esquema Backend §5.2). Si la fila no existe todavia, no hay nada
        # que bloquear -pero entonces el stock disponible es 0 de por si, no
        # hay carrera real posible con una fila que no existe.
        Stock.objects.select_for_update().filter(
            variant=variant, warehouse=warehouse
        ).first()
        available = ReservationService.get_available_quantity(
            variant=variant, warehouse=warehouse
        )
        if available < quantity:
            raise InsufficientStockError(
                sku=variant.sku, available=available, requested=quantity
            )
        reservation = ProductReservation.objects.create(
            customer=customer,
            variant=variant,
            warehouse=warehouse,
            quantity=quantity,
            expires_at=expires_at,
            user=user,
        )

        from usuarios.services import AuditLogService

        AuditLogService.log_action(
            user=user,
            action="RESERVATION_CREATED",
            entity="ProductReservation",
            entity_id=reservation.id,
            details={
                "customer_id": customer.id,
                "variant_id": variant.id,
                "warehouse_id": warehouse.id,
                "quantity": str(quantity),
                "expires_at": str(expires_at),
            },
        )
        return reservation

    @staticmethod
    @transaction.atomic
    def cancel_reservation(
        *, reservation: ProductReservation, user
    ) -> ProductReservation:
        if reservation.status != "ACTIVE":
            raise ReservationNotActiveError()
        reservation.status = "CANCELLED"
        reservation.save(update_fields=["status"])

        from usuarios.services import AuditLogService

        AuditLogService.log_action(
            user=user,
            action="RESERVATION_CANCELLED",
            entity="ProductReservation",
            entity_id=reservation.id,
        )
        return reservation

    @staticmethod
    @transaction.atomic
    def convert_to_sale(
        *,
        reservation: ProductReservation,
        cash_session: CashSession,
        user,
        payments: list[dict],
    ) -> Sale:
        if reservation.status != "ACTIVE":
            raise ReservationNotActiveError()

        sale = SaleService.create_sale(
            customer=reservation.customer,
            cash_session=cash_session,
            user=user,
            lines=[
                {
                    "variant_id": reservation.variant_id,
                    "quantity": reservation.quantity,
                }
            ],
            payments=payments,
        )
        reservation.status = "CONVERTED"
        reservation.sale = sale
        reservation.save(update_fields=["status", "sale"])

        from usuarios.services import AuditLogService

        AuditLogService.log_action(
            user=user,
            action="RESERVATION_CONVERTED",
            entity="ProductReservation",
            entity_id=reservation.id,
            details={"sale_id": sale.id},
        )
        return sale

    @staticmethod
    def expire_overdue_reservations(*, at=None) -> int:
        """Usado por la tarea periodica de Celery (ventas.tasks) -una
        reserva vencida libera el stock automaticamente sin intervencion
        manual, porque get_available_quantity() solo resta las ACTIVE."""
        at = at or timezone.now()
        return ProductReservation.objects.filter(
            status="ACTIVE", expires_at__lt=at
        ).update(status="EXPIRED")
