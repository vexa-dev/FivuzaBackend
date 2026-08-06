import uuid
from decimal import Decimal

import boto3
from django.conf import settings
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone
from rest_framework.exceptions import APIException, ValidationError

from inventario.models import ProductVariant, Stock
from inventario.services import StockService
from ventas.models import (
    CashMovement,
    CashRegister,
    CashSession,
    Promotion,
    Sale,
    SaleDetail,
    SalePayment,
)

# Comprobantes de movimientos de caja: mismo patron de URL prefirmada de S3
# que inventario.services.MediaService, pero self-contenido aqui -un
# CashMovement no existe todavia cuando se pide la URL (a diferencia de una
# ProductVariant, que ya tiene id antes de subir su imagen), asi que la key
# se genera con un uuid propio en vez de depender de un pk existente.
_PRESIGNED_URL_TTL_SECONDS = 300
_ALLOWED_RECEIPT_CONTENT_TYPES = {
    "image/jpeg",
    "image/png",
    "image/webp",
    "application/pdf",
}


class CashSessionAlreadyOpenError(APIException):
    status_code = 409
    default_code = "CASH_SESSION_ALREADY_OPEN"
    default_detail = {
        "error": {
            "code": "CASH_SESSION_ALREADY_OPEN",
            "message": "Esta caja ya tiene una sesion abierta.",
        }
    }


class CashSessionNotOpenError(APIException):
    status_code = 409
    default_code = "CASH_SESSION_NOT_OPEN"
    default_detail = {
        "error": {
            "code": "CASH_SESSION_NOT_OPEN",
            "message": "Esta sesion de caja ya esta cerrada.",
        }
    }


class CashSessionService:
    """Apertura/cierre de caja con arqueo (Especificacion de API §4.4;
    Esquema Backend §7.2). Una caja fisica (CashRegister) no puede tener dos
    sesiones abiertas a la vez -es la regla que hace que "que caja esta
    usando cada cajero ahora mismo" sea una pregunta con una sola respuesta."""

    @staticmethod
    def open_session(
        *, cash_register: CashRegister, user, opening_amount
    ) -> CashSession:
        if CashSession.objects.filter(
            cash_register=cash_register, status="OPEN"
        ).exists():
            raise CashSessionAlreadyOpenError()

        return CashSession.objects.create(
            cash_register=cash_register,
            user=user,
            opening_amount=opening_amount,
            opening_at=timezone.now(),
            status="OPEN",
        )

    @staticmethod
    def close_session(
        *,
        session: CashSession,
        counted_closing_amount,
        user,
        tenant=None,
        notes: str | None = None,
    ) -> CashSession:
        if session.status != "OPEN":
            raise CashSessionNotOpenError()

        expected = CashSessionService._calculate_expected_closing_amount(session)
        session.expected_closing_amount = expected
        session.counted_closing_amount = counted_closing_amount
        session.difference = counted_closing_amount - expected
        session.status = "CLOSED"
        session.closing_at = timezone.now()
        if notes:
            session.notes = notes
        session.save(
            update_fields=[
                "expected_closing_amount",
                "counted_closing_amount",
                "difference",
                "status",
                "closing_at",
                "notes",
            ]
        )

        from usuarios.services import AuditLogService

        AuditLogService.log_action(
            user=user,
            action="CASH_SESSION_CLOSED",
            entity="CashSession",
            entity_id=session.id,
            details={
                "expected_closing_amount": str(expected),
                "counted_closing_amount": str(counted_closing_amount),
                "difference": str(session.difference),
            },
        )

        if tenant is not None:
            CashSessionService._maybe_alert_on_difference(
                session=session, tenant=tenant
            )

        return session

    @staticmethod
    def _maybe_alert_on_difference(*, session: CashSession, tenant) -> None:
        from core.models import TenantSettings

        threshold = TenantSettings.objects.get(
            tenant=tenant
        ).cash_difference_alert_threshold
        if abs(session.difference) <= threshold:
            return

        from ventas.tasks import send_cash_difference_alert

        send_cash_difference_alert.delay(tenant.schema_name, session.id)

    @staticmethod
    def _calculate_expected_closing_amount(session: CashSession):
        # Ventas al contado (SalePayment.method=CASH) todavia no se pueden
        # crear -SaleService llega en un sprint posterior- pero la relacion
        # Sale.cash_session ya existe en el modelo (BDD v5), asi que se
        # incluye desde ya: el dia que el POS exista, el arqueo ya calcula
        # bien sin tocar este metodo.
        from ventas.models import SalePayment

        cash_sales = (
            SalePayment.objects.filter(
                method="CASH", sale__cash_session=session
            ).aggregate(total=Sum("amount"))["total"]
            or 0
        )
        movements_in = (
            session.movements.filter(type="IN").aggregate(total=Sum("amount"))["total"]
            or 0
        )
        movements_out = (
            session.movements.filter(type="OUT").aggregate(total=Sum("amount"))["total"]
            or 0
        )
        return session.opening_amount + cash_sales + movements_in - movements_out

    @staticmethod
    def add_movement(
        *,
        session: CashSession,
        type: str,
        concept: str,
        amount,
        user,
        reason: str = "",
        receipt_url: str | None = None,
    ) -> CashMovement:
        if session.status != "OPEN":
            raise CashSessionNotOpenError()

        return CashMovement.objects.create(
            cash_session=session,
            type=type,
            concept=concept,
            amount=amount,
            user=user,
            reason=reason,
            receipt_url=receipt_url,
        )


class CashMovementReceiptService:
    """URLs prefirmadas de S3 para el comprobante de un movimiento de caja
    (Convenciones §5.1) -mismo patron que inventario.services.MediaService."""

    @staticmethod
    def build_receipt_upload_url(content_type: str) -> dict:
        if content_type not in _ALLOWED_RECEIPT_CONTENT_TYPES:
            raise ValueError(f"Tipo de archivo no permitido: {content_type}")

        extension = content_type.split("/")[-1]
        key = f"cash-movement-receipts/{uuid.uuid4()}.{extension}"

        client = boto3.client("s3", region_name=settings.AWS_S3_REGION)
        upload_url = client.generate_presigned_url(
            "put_object",
            Params={
                "Bucket": settings.AWS_STORAGE_BUCKET_NAME,
                "Key": key,
                "ContentType": content_type,
            },
            ExpiresIn=_PRESIGNED_URL_TTL_SECONDS,
        )
        receipt_url = (
            f"https://{settings.AWS_STORAGE_BUCKET_NAME}.s3."
            f"{settings.AWS_S3_REGION}.amazonaws.com/{key}"
        )
        return {"upload_url": upload_url, "receipt_url": receipt_url}


class PromotionService:
    """Resuelve la promocion vigente aplicable a una variante en una fecha
    dada (Esquema Backend §6.2). El POS (Sprint 15+) la usara para calcular
    el descuento de cada linea del carrito al vuelo.

    Reglas de prioridad (sin una cifra "oficial" documentada, se asume lo
    siguiente como razonable y determinista):
    1. Una promocion dirigida a la variante especifica gana sobre una que
       solo apunta a su categoria -mas especifico gana.
    2. Si hay mas de una promocion vigente al mismo nivel de especificidad,
       gana la mas reciente (id mas alto). No se comparan los `value` entre
       si porque PERCENTAGE y FIXED_AMOUNT no son magnitudes comparables."""

    @staticmethod
    def resolve_active_promotion(*, variant, at=None) -> Promotion | None:
        at = at or timezone.now()
        active = Promotion.objects.filter(
            is_active=True, start_date__lte=at, end_date__gte=at
        )

        direct = active.filter(targets__variant=variant).order_by("-id").first()
        if direct is not None:
            return direct

        return (
            active.filter(targets__category=variant.product.category)
            .order_by("-id")
            .first()
        )


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
        at=None,
    ) -> Sale:
        if cash_session.status != "OPEN":
            raise NoCashSessionError()

        at = at or timezone.now()
        warehouse = cash_session.cash_register.warehouse

        subtotal = Decimal("0")
        discount_total = Decimal("0")
        prepared_lines = []
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
            if current_quantity < quantity:
                raise InsufficientStockError(
                    sku=variant.sku, available=current_quantity, requested=quantity
                )

            unit_price = variant.price
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
            )

        for payment in payments:
            SalePayment.objects.create(
                sale=sale, method=payment["method"], amount=payment["amount"]
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

        return sale

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
