from decimal import Decimal

from django.db import transaction
from django.db.models import Sum
from rest_framework.exceptions import ValidationError

from core.decimals import round2
from inventario.models import ProductVariant, Stock
from inventario.services import StockService
from ventas.models import (
    CashSession,
    CustomerBalanceLedger,
    Sale,
    SaleDetail,
    SaleReturn,
    SaleReturnDetail,
)
from ventas.services.cash import CashSessionService
from ventas.services.sales import (
    NoCashSessionError,
    ReturnExceedsSoldError,
    SaleNotCompletedError,
)


class ReturnService:
    """Devolucion (Sprint 18, Plan de Implementacion): a diferencia de
    void_sale(), asume que la venta fue correcta -el cliente trajo
    mercaderia de vuelta, dias o semanas despues. Por eso NO exige que la
    sesion de caja de la VENTA ORIGINAL siga abierta (ya pudo cerrar hace
    tiempo); un reembolso en efectivo sale de la caja ABIERTA AHORA que
    recibe al cliente, no de la que cobro la venta."""

    @staticmethod
    @transaction.atomic
    def create_return(
        *,
        sale: Sale,
        items: list[dict],
        reason: str,
        refund_type: str,
        user,
        cash_session: CashSession | None = None,
        authorization_token: str | None = None,
    ) -> SaleReturn:
        # Bloque C.1: sin SALES_RETURN propio, la devolucion sale con la
        # autorizacion de un supervisor para esta venta.
        from usuarios.authorization import SupervisorAuthorizationService

        authorization = SupervisorAuthorizationService.require(
            user=user,
            permission="SALES_RETURN",
            raw_token=authorization_token,
            target_id=sale.id,
        )
        if sale.status != "COMPLETED":
            raise SaleNotCompletedError()
        if refund_type == "CASH" and (
            cash_session is None or cash_session.status != "OPEN"
        ):
            raise NoCashSessionError()

        total_refund_amount = Decimal("0")
        prepared_items = []
        for item in items:
            try:
                sale_detail = SaleDetail.objects.get(
                    id=item["sale_detail_id"], sale=sale
                )
            except SaleDetail.DoesNotExist as exc:
                raise ValidationError(
                    f"La linea {item['sale_detail_id']} no pertenece a esta venta."
                ) from exc
            returned = SaleReturnDetail.objects.filter(
                sale_detail=sale_detail
            ).aggregate(quantity=Sum("quantity_returned"), refunded=Sum("subtotal"))
            already_returned = returned["quantity"] or Decimal("0")
            already_refunded = returned["refunded"] or Decimal("0")
            available = sale_detail.quantity - already_returned
            quantity_returned = round2(item["quantity_returned"])
            if quantity_returned > available:
                raise ReturnExceedsSoldError(
                    sku=sale_detail.sku_snapshot,
                    available=available,
                    requested=quantity_returned,
                )

            # Proporcional al precio ya neto de descuento de la linea
            # original -no al unit_price bruto, para que devolver media
            # linea con descuento reembolse la mitad de lo que el cliente
            # realmente pago, no de lista.
            #
            # Se reembolsa en centimos (ROUND_HALF_UP, misma regla que la
            # venta). Redondear cada devolucion parcial por separado puede
            # descuadrar la suma (3 x 3.33 de una linea de 10.00 = 9.99),
            # asi que la devolucion que completa la linea se lleva el resto
            # exacto: entre todas reembolsan justo lo que se cobro. El tope
            # evita que muchas parciales redondeadas hacia arriba (20 x 0.005
            # -> 0.01) pasen lo que queda por reembolsar de la linea.
            remaining = round2(sale_detail.subtotal - already_refunded)
            if quantity_returned == available:
                subtotal = remaining
            else:
                subtotal = min(
                    round2(
                        sale_detail.subtotal * quantity_returned / sale_detail.quantity
                    ),
                    remaining,
                )
            prepared_items.append(
                {
                    "sale_detail": sale_detail,
                    "quantity_returned": quantity_returned,
                    "restock": item.get("restock", True),
                    "subtotal": subtotal,
                }
            )
            total_refund_amount += subtotal

        sale_return = SaleReturn.objects.create(
            sale=sale,
            user=user,
            reason=reason,
            total_refund_amount=total_refund_amount,
            refund_type=refund_type,
        )

        for prepared in prepared_items:
            sale_detail = prepared["sale_detail"]
            SaleReturnDetail.objects.create(
                sale_return=sale_return,
                sale_detail=sale_detail,
                quantity_returned=prepared["quantity_returned"],
                restock=prepared["restock"],
                subtotal=prepared["subtotal"],
            )
            if prepared["restock"]:
                try:
                    variant = ProductVariant.objects.get(id=sale_detail.variant_id)
                except ProductVariant.DoesNotExist:
                    continue
                stock = (
                    Stock.objects.select_for_update()
                    .filter(variant=variant, warehouse=sale.warehouse)
                    .first()
                )
                current_quantity = stock.quantity if stock else Decimal("0")
                StockService.adjust_stock(
                    variant=variant,
                    warehouse=sale.warehouse,
                    counted_quantity=current_quantity + prepared["quantity_returned"],
                    concept="RETURN",
                    user=user,
                    audit=False,
                )

        if refund_type == "CASH":
            CashSessionService.add_movement(
                session=cash_session,
                type="OUT",
                concept="DEVOLUCION",
                amount=total_refund_amount,
                user=user,
                reason=f"Devolucion de venta {sale.invoice_number}",
            )
        else:
            CustomerBalanceLedger.objects.create(
                customer=sale.customer,
                sale_return=sale_return,
                sale=sale,
                type="CREDIT",
                amount=total_refund_amount,
                description=f"Devolucion de venta {sale.invoice_number}",
            )

        from usuarios.services import AuditLogService

        AuditLogService.log_action(
            user=user,
            action="SALE_RETURNED",
            entity="SaleReturn",
            entity_id=sale_return.id,
            details={
                "sale_id": sale.id,
                "invoice_number": sale.invoice_number,
                "total_refund_amount": str(total_refund_amount),
                "refund_type": refund_type,
                **SupervisorAuthorizationService.audit_details(authorization),
            },
        )

        return sale_return
