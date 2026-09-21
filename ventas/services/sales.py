import uuid
from decimal import Decimal

from django.db import transaction
from django.db.models import Sum
from django.template.loader import render_to_string
from django.utils import timezone
from rest_framework.exceptions import APIException, ValidationError

from inventario.models import ProductVariant, Stock
from inventario.services import StockService
from ventas.models import (
    CashSession,
    Sale,
    SaleDetail,
    SalePayment,
)
from ventas.services.cash import CashSessionService
from ventas.services.credit import CreditLedgerService
from ventas.services.promotions import PromotionService


class InsufficientStockError(APIException):
    status_code = 409
    default_code = "INSUFFICIENT_STOCK"

    def __init__(self, *, sku: str, available: Decimal, requested: Decimal):
        super().__init__(
            {
                "error": {
                    "code": "INSUFFICIENT_STOCK",
                    "message": (
                        f"Stock insuficiente para {sku}: disponible {available}, "
                        f"solicitado {requested}."
                    ),
                }
            }
        )


class PaymentMismatchError(APIException):
    status_code = 409
    default_code = "PAYMENT_MISMATCH"
    default_detail = {
        "error": {
            "code": "PAYMENT_MISMATCH",
            "message": "La suma de los pagos no coincide con el total de la venta.",
        }
    }


class NoCashSessionError(APIException):
    status_code = 409
    default_code = "NO_CASH_SESSION"
    default_detail = {
        "error": {
            "code": "NO_CASH_SESSION",
            "message": "No hay una sesion de caja abierta para registrar la venta.",
        }
    }


class SaleNotCompletedError(APIException):
    status_code = 409
    default_code = "SALE_NOT_COMPLETED"
    default_detail = {
        "error": {
            "code": "SALE_NOT_COMPLETED",
            "message": "Esta venta ya esta anulada.",
        }
    }


class SaleHasReturnsError(APIException):
    status_code = 409
    default_code = "SALE_HAS_RETURNS"
    default_detail = {
        "error": {
            "code": "SALE_HAS_RETURNS",
            "message": "No se puede anular una venta que ya tiene devoluciones registradas.",
        }
    }


class CashSessionClosedError(APIException):
    status_code = 409
    default_code = "CASH_SESSION_CLOSED"
    default_detail = {
        "error": {
            "code": "CASH_SESSION_CLOSED",
            "message": "No se puede anular una venta de una sesion de caja ya cerrada.",
        }
    }


class ReturnExceedsSoldError(APIException):
    status_code = 409
    default_code = "RETURN_EXCEEDS_SOLD"

    def __init__(self, *, sku: str, available: Decimal, requested: Decimal):
        super().__init__(
            {
                "error": {
                    "code": "RETURN_EXCEEDS_SOLD",
                    "message": (
                        f"No se puede devolver mas de lo vendido para {sku}: "
                        f"disponible para devolver {available}, solicitado {requested}."
                    ),
                }
            }
        )


class SaleService:
    """SaleService.create_sale(): el endpoint mas complejo del proyecto
    (Esquema Backend §6.2; API Spec §4.1). Todo ocurre en una sola
    transaccion atomica -si cualquier linea falla (stock insuficiente) o los
    pagos no cuadran, no queda ningun efecto parcial (ni Sale, ni
    SaleDetail, ni movimiento de stock, ni SalePayment).

    Decisiones asumidas, sin una cifra/regla "oficial" documentada:
    - El almacen de la venta se deriva de cash_session.cash_register.warehouse
      (no se pide aparte): una caja pertenece a un unico almacen, pedirlo
      dos veces solo abre la puerta a que no coincidan.
    - Snapshot de "impuesto" por linea (mencionado en el Plan de
      Implementacion) queda deferido: TaxRate ya trae su propio comentario
      desde el Sprint 5 ("no calcula ni desglosa impuestos todavia") y
      SaleDetail (BDD v5) no tiene un campo para ese desglose -agregarlo
      hoy seria diseñar para un requisito que todavia no esta especificado.
    - Sin descuento manual explicito por linea, se resuelve automaticamente
      la promocion vigente via PromotionService; si el caller SI manda
      discount_amount, ese valor manual gana (el cajero puede anular el
      descuento automatico).
    - payment_status siempre queda en PAID y status en COMPLETED: la unica
      forma de crear una venta hoy es que los pagos cuadren exactamente con
      el total (PAYMENT_MISMATCH en caso contrario); PARTIAL/UNPAID y el
      credito/fiado (CREDIT_LEDGER/BALANCE contra CustomerDebtLedger/
      CustomerBalanceLedger) son responsabilidad de un sprint posterior
      (Fase 3, credito/fiado) -este sprint solo persiste el SalePayment,
      sin tocar esos libros todavia.
    - invoice_number es un correlativo simple (`V-000123`), sin intentar
      cumplir un esquema fiscal real (boleta/factura electronica SUNAT) -no
      hay ningun sprint de facturacion electronica en el plan todavia.
    """

    @staticmethod
    @transaction.atomic
    def create_sale(
        *,
        customer,
        cash_session: CashSession,
        user,
        lines: list[dict],
        payments: list[dict],
        client_side_uuid: str | None = None,
        allow_oversell: bool = False,
        at=None,
    ) -> Sale:
        if cash_session.status != "OPEN":
            raise NoCashSessionError()
        # Bloque A.2: vender en la caja de otro le descuadra el arqueo a esa
        # persona. Se valida en el servicio y no solo en la vista para que
        # el sync offline pase por la misma regla.
        CashSessionService.assert_can_sell(session=cash_session, user=user)

        at = at or timezone.now()
        warehouse = cash_session.cash_register.warehouse

        subtotal = Decimal("0")
        discount_total = Decimal("0")
        prepared_lines = []
        oversold_variant_ids: list[int] = []
        for line in lines:
            try:
                variant = ProductVariant.objects.select_related("product").get(
                    id=line["variant_id"]
                )
            except ProductVariant.DoesNotExist as exc:
                raise ValidationError(
                    f"La variante {line['variant_id']} no existe."
                ) from exc
            quantity = Decimal(str(line["quantity"]))

            # Mismo patron que PurchaseService.receive_order (Sprint 5): se
            # lee el stock BAJO el lock de select_for_update, para que el
            # chequeo de disponibilidad y el ajuste posterior operen sobre el
            # mismo valor, sin ventana para que otra venta concurrente se
            # cuele entre medio.
            stock = (
                Stock.objects.select_for_update()
                .filter(variant=variant, warehouse=warehouse)
                .first()
            )
            current_quantity = stock.quantity if stock else Decimal("0")
            oversold = current_quantity < quantity
            if oversold:
                # Sprint 20: una venta sincronizada desde el POS offline no
                # se rechaza por falta de stock -el producto ya salio
                # fisicamente de la tienda cuando el cajero la cobro sin
                # conexion. Se registra igual y el movimiento queda marcado
                # (oversell_flag) para que el dueño la revise despues.
                if not allow_oversell:
                    raise InsufficientStockError(
                        sku=variant.sku, available=current_quantity, requested=quantity
                    )
                oversold_variant_ids.append(variant.id)

            # Sprint 28: una cotizacion aceptada convierte con sus precios
            # ya congelados (QuoteService.convert_to_sale) -si la linea trae
            # unit_price explicito, gana sobre la resolucion normal (precio
            # de catalogo/tramo por volumen). Ningun caller existente manda
            # esta clave, asi que el comportamiento por defecto no cambia.
            unit_price = line.get("unit_price")
            unit_price = (
                Decimal(str(unit_price))
                if unit_price is not None
                else SaleService._resolve_unit_price(variant=variant, quantity=quantity)
            )
            line_subtotal = unit_price * quantity

            discount_amount = line.get("discount_amount")
            if discount_amount is None:
                discount_amount = SaleService._resolve_promotion_discount(
                    variant=variant, quantity=quantity, unit_price=unit_price, at=at
                )
            else:
                discount_amount = min(Decimal(str(discount_amount)), line_subtotal)

            prepared_lines.append(
                {
                    "variant": variant,
                    "quantity": quantity,
                    "unit_price": unit_price,
                    "discount_amount": discount_amount,
                    "subtotal": line_subtotal - discount_amount,
                    "current_quantity": current_quantity,
                    "oversold": oversold,
                }
            )
            subtotal += line_subtotal
            discount_total += discount_amount

        total = subtotal - discount_total
        payments_total = sum((p["amount"] for p in payments), Decimal("0"))
        if payments_total != total:
            raise PaymentMismatchError()

        sale = Sale.objects.create(
            invoice_number=SaleService._next_invoice_number(),
            customer=customer,
            user=user,
            warehouse=warehouse,
            cash_session=cash_session,
            subtotal=subtotal,
            discount_total=discount_total,
            total=total,
            payment_status="PAID",
            status="COMPLETED",
            client_side_uuid=client_side_uuid or uuid.uuid4().hex,
            sync_status="SYNCED",
            occurred_at=at,
        )

        for prepared in prepared_lines:
            variant = prepared["variant"]
            SaleDetail.objects.create(
                sale=sale,
                variant_id=variant.id,
                product_name_snapshot=variant.product.name,
                sku_snapshot=variant.sku,
                quantity=prepared["quantity"],
                unit_price=prepared["unit_price"],
                discount_amount=prepared["discount_amount"],
                subtotal=prepared["subtotal"],
            )
            StockService.adjust_stock(
                variant=variant,
                warehouse=warehouse,
                counted_quantity=prepared["current_quantity"] - prepared["quantity"],
                concept="SALE",
                user=user,
                audit=False,
                oversell_flag=prepared["oversold"],
            )

        for payment in payments:
            SalePayment.objects.create(
                sale=sale, method=payment["method"], amount=payment["amount"]
            )
            # CreditLedgerService.register_*() puede levantar
            # CreditLimitExceededError/InsufficientBalanceError -al estar
            # todo dentro de esta misma transaccion atomica, el rollback
            # deshace tambien el Sale/SaleDetail/movimiento de stock ya
            # creados en este mismo request (Sprint 19).
            if payment["method"] == "CREDIT_LEDGER":
                CreditLedgerService.register_credit_sale(
                    customer=customer, sale=sale, amount=payment["amount"]
                )
            elif payment["method"] == "BALANCE":
                CreditLedgerService.register_balance_use(
                    customer=customer, sale=sale, amount=payment["amount"]
                )

        from usuarios.services import AuditLogService

        AuditLogService.log_action(
            user=user,
            action="SALE_CREATED",
            entity="Sale",
            entity_id=sale.id,
            details={
                "invoice_number": sale.invoice_number,
                "total": str(total),
                "lines": len(prepared_lines),
            },
        )

        # Atributo transitorio (no persiste en el modelo): SaleSyncService lo
        # lee para armar el array "conflicts" de la respuesta de /sales/sync/
        # sin cambiar la firma de retorno que ya usan el endpoint normal de
        # creacion y todos sus tests existentes.
        sale.oversold_variant_ids = oversold_variant_ids

        from django.db import connection

        from dashboard.services import DashboardBroadcastService

        # Sprint 24 (TRD §2.5): empuja la venta al dashboard conectado por
        # WebSocket. connection.schema_name es el tenant actual -ya resuelto
        # por TenantMainMiddleware antes de llegar aqui. Si no hay listeners
        # conectados (channel_layer sin grupo activo) esto no falla ni hace
        # nada visible, es un group_send normal.
        DashboardBroadcastService.broadcast_sale_completed(
            schema_name=connection.schema_name,
            warehouse_id=sale.warehouse_id,
            total=sale.total,
        )

        return sale

    @staticmethod
    @transaction.atomic
    def void_sale(sale: Sale, *, reason: str, user) -> Sale:
        """Anulacion (Sprint 18, Plan de Implementacion): la venta nunca
        debio existir -a diferencia de una devolucion (ReturnService), que
        asume que la venta fue correcta y el cliente simplemente trajo la
        mercaderia de vuelta. Por eso exige que la sesion de caja siga
        abierta (es "deshacer algo que acaba de pasar", no un flujo que
        pueda ocurrir dias despues) y reingresa el 100% del stock.

        Desde el Sprint 19 tambien revierte CREDIT_LEDGER (perdona la deuda)
        y BALANCE (devuelve el saldo a favor consumido) via
        CreditLedgerService -antes quedaba deferido porque esos libros ni
        se escribian todavia al vender."""
        if sale.status != "COMPLETED":
            raise SaleNotCompletedError()
        if sale.returns.exists():
            raise SaleHasReturnsError()
        if sale.cash_session is not None and sale.cash_session.status != "OPEN":
            raise CashSessionClosedError()

        for detail in sale.details.all():
            try:
                variant = ProductVariant.objects.select_related("product").get(
                    id=detail.variant_id
                )
            except ProductVariant.DoesNotExist:
                # La variante pudo haberse borrado (baja logica no aplica a
                # ProductVariant hoy) despues de la venta -sale_details ya
                # tiene su snapshot, asi que la anulacion no se bloquea por
                # esto, simplemente no hay a donde reingresar el stock.
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
                counted_quantity=current_quantity + detail.quantity,
                concept="RETURN",
                user=user,
                audit=False,
            )

        cash_amount = sale.payments.filter(method="CASH").aggregate(
            total=Sum("amount")
        )["total"] or Decimal("0")
        if cash_amount > 0 and sale.cash_session is not None:
            CashSessionService.add_movement(
                session=sale.cash_session,
                type="OUT",
                concept="DEVOLUCION",
                amount=cash_amount,
                user=user,
                reason=f"Anulacion de venta {sale.invoice_number}",
            )

        credit_amount = sale.payments.filter(method="CREDIT_LEDGER").aggregate(
            total=Sum("amount")
        )["total"] or Decimal("0")
        if credit_amount > 0:
            CreditLedgerService.reverse_credit_sale(
                customer=sale.customer, sale=sale, amount=credit_amount
            )

        balance_amount = sale.payments.filter(method="BALANCE").aggregate(
            total=Sum("amount")
        )["total"] or Decimal("0")
        if balance_amount > 0:
            CreditLedgerService.reverse_balance_use(
                customer=sale.customer, sale=sale, amount=balance_amount
            )

        sale.status = "VOIDED"
        sale.save(update_fields=["status"])

        from usuarios.services import AuditLogService

        AuditLogService.log_action(
            user=user,
            action="SALE_VOIDED",
            entity="Sale",
            entity_id=sale.id,
            details={"invoice_number": sale.invoice_number, "reason": reason},
        )

        return sale

    @staticmethod
    def _resolve_unit_price(*, variant, quantity: Decimal) -> Decimal:
        """Precio por volumen (Sprint 26, BDD v5 "volume_pricing_tiers"):
        se resuelve el tramo de mayor min_quantity que la cantidad vendida
        alcance a cubrir -no el mas barato ni el mas cercano, el mas
        especifico. Si ninguno aplica, product_variants.price sin cambios.
        Corre ANTES que las promociones -una promocion sigue pudiendo
        aplicar un descuento adicional sobre el precio mayorista ya resuelto."""
        from inventario.models import VolumePricingTier

        tier = (
            VolumePricingTier.objects.filter(
                variant=variant, min_quantity__lte=quantity
            )
            .order_by("-min_quantity")
            .first()
        )
        return tier.unit_price if tier else variant.price

    @staticmethod
    def _resolve_promotion_discount(
        *, variant, quantity: Decimal, unit_price: Decimal, at
    ) -> Decimal:
        promotion = PromotionService.resolve_active_promotion(variant=variant, at=at)
        if promotion is None:
            return Decimal("0")

        line_subtotal = unit_price * quantity
        if promotion.type == "PERCENTAGE":
            return line_subtotal * promotion.value / Decimal("100")
        # FIXED_AMOUNT: monto fijo por unidad, nunca mas que el subtotal de
        # la linea (sin regla documentada sobre si escala con la cantidad;
        # se asume por unidad, tope al subtotal para no dejarlo negativo).
        return min(promotion.value * quantity, line_subtotal)

    @staticmethod
    def _next_invoice_number() -> str:
        return f"V-{Sale.objects.count() + 1:06d}"


class SaleNotFoundError(APIException):
    status_code = 404
    default_code = "NOT_FOUND"
    default_detail = {"error": {"code": "NOT_FOUND", "message": "Venta no encontrada."}}


class ReceiptService:
    """Boleta/ticket no fiscal (API Spec §4.11): HTML de ancho fijo listo
    para window.print() en el frontend. Sin campo ni tabla nueva en la BDD
    v5 -se arma con datos ya existentes de Sale/SaleDetail/SalePayment y
    Tenant (company_name, ruc), por diseño (ver nota de la spec)."""

    _CHARS_PER_MM = {58: 32, 80: 48}

    @staticmethod
    def render_html(sale: Sale, tenant, width_mm: int = 58) -> str:
        columns = ReceiptService._CHARS_PER_MM.get(width_mm, 32)
        lines = [
            ReceiptService._format_detail_line(detail, columns)
            for detail in sale.details.all()
        ]
        payments_line = " / ".join(
            f"{payment.method}: {payment.amount.quantize(Decimal('0.01'))}"
            for payment in sale.payments.all()
        )
        return render_to_string(
            "ventas/receipts/sale_receipt.html",
            {
                "company_name": tenant.company_name,
                "ruc": tenant.ruc or "",
                "sale": sale,
                "issued_at": timezone.localtime(sale.occurred_at).strftime(
                    "%d/%m/%Y %H:%M"
                ),
                "lines": lines,
                "total": sale.total.quantize(Decimal("0.01")),
                "payments_line": payments_line,
                "width_mm": width_mm,
            },
        )

    @staticmethod
    def _format_detail_line(detail: SaleDetail, columns: int) -> dict:
        quantity = detail.quantity
        if quantity == quantity.to_integral_value():
            qty_label = f"{int(quantity)} x"
        else:
            qty_label = f"{quantity.normalize()}"
        left = f"{qty_label} {detail.product_name_snapshot}"
        right = f"{detail.subtotal.quantize(Decimal('0.01'))}"
        padding = max(columns - len(left) - len(right), 1)
        return {"label": f"{left} {'.' * padding} {right}"}
