import uuid
from decimal import Decimal

import boto3
from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Q, Sum
from django.template.loader import render_to_string
from django.utils import timezone
from rest_framework.exceptions import APIException, ValidationError

from inventario.models import ProductVariant, Stock
from inventario.services import StockService
from ventas.models import (
    CashMovement,
    CashRegister,
    CashSession,
    CustomerBalanceLedger,
    CustomerDebtLedger,
    Promotion,
    Sale,
    SaleDetail,
    SalePayment,
    SaleReturn,
    SaleReturnDetail,
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

    @staticmethod
    def build_active_promotion_index(
        *, at=None
    ) -> tuple[dict[int, Promotion], dict[int, Promotion]]:
        """Misma regla de prioridad que resolve_active_promotion(), pero
        precalculada de una sola pasada para N variantes -evita el problema
        N+1 de llamar resolve_active_promotion() en un loop (POSCatalogService
        sirve el catalogo completo de un tenant, Sprint 16, TRD §4.4).
        Devuelve (variant_id -> Promotion, category_id -> Promotion);
        order_by("-id") hace que dict.setdefault() se quede con la promocion
        mas reciente en cada bucket, igual que el "-id" de arriba."""
        at = at or timezone.now()
        promotions = (
            Promotion.objects.filter(
                is_active=True, start_date__lte=at, end_date__gte=at
            )
            .order_by("-id")
            .prefetch_related("targets")
        )

        by_variant: dict[int, Promotion] = {}
        by_category: dict[int, Promotion] = {}
        for promotion in promotions:
            for target in promotion.targets.all():
                if target.variant_id is not None:
                    by_variant.setdefault(target.variant_id, promotion)
                elif target.category_id is not None:
                    by_category.setdefault(target.category_id, promotion)
        return by_variant, by_category


class POSCatalogService:
    """Catalogo y busqueda optimizados para el POS (Sprint 16, Esquema
    Backend §6.2): payload reducido (variante, precio, stock, promocion
    vigente) pensado para cachearse en el cliente -base del futuro modo
    offline. Vive en ventas (no en inventario) porque combina datos de
    Stock/ProductVariant con PromotionService, que es un concepto propio
    de ventas; inventario nunca importa de ventas (capa inferior)."""

    @staticmethod
    def search(*, warehouse, query: str) -> list[dict]:
        # Prioridad de escaneo: un codigo de barras exacto resuelve de
        # inmediato, sin competir con resultados de texto parecido -si el
        # escaneo no encuentra nada, recien ahi se cae a busqueda difusa
        # por nombre/sku. Sin search_vector (mismo alcance ya documentado
        # en Sprint 14: la columna existe en el modelo pero ningun punto
        # del codigo la popula todavia), se usa icontains, consistente con
        # el resto del catalogo de inventario.
        exact = list(
            ProductVariant.objects.filter(
                is_active=True,
                product__is_for_sale=True,
                product__is_active=True,
                barcode=query,
            ).select_related("product")
        )
        if exact:
            variants = exact
        else:
            variants = list(
                ProductVariant.objects.filter(
                    is_active=True, product__is_for_sale=True, product__is_active=True
                )
                .filter(Q(sku__icontains=query) | Q(product__name__icontains=query))
                .select_related("product")
                .order_by("product__name")[:20]
            )
        return POSCatalogService._serialize(variants, warehouse)

    @staticmethod
    def catalog(*, warehouse) -> list[dict]:
        variants = (
            ProductVariant.objects.filter(
                is_active=True, product__is_for_sale=True, product__is_active=True
            )
            .select_related("product")
            .order_by("product__name")
        )
        return POSCatalogService._serialize(list(variants), warehouse)

    @staticmethod
    def _serialize(variants: list[ProductVariant], warehouse) -> list[dict]:
        variant_ids = [variant.id for variant in variants]
        stock_by_variant = dict(
            Stock.objects.filter(
                warehouse=warehouse, variant_id__in=variant_ids
            ).values_list("variant_id", "quantity")
        )
        by_variant, by_category = PromotionService.build_active_promotion_index()

        results = []
        for variant in variants:
            promotion = by_variant.get(variant.id) or by_category.get(
                variant.product.category_id
            )
            results.append(
                {
                    "id": variant.id,
                    "sku": variant.sku,
                    "barcode": variant.barcode,
                    "product_name": variant.product.name,
                    "price": str(variant.price),
                    "stock": str(stock_by_variant.get(variant.id, Decimal("0"))),
                    "promotion": (
                        {
                            "id": promotion.id,
                            "name": promotion.name,
                            "type": promotion.type,
                            "value": str(promotion.value),
                        }
                        if promotion is not None
                        else None
                    ),
                }
            )
        return results


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
                "issued_at": timezone.localtime(sale.created_at).strftime(
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
    ) -> SaleReturn:
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
            already_returned = SaleReturnDetail.objects.filter(
                sale_detail=sale_detail
            ).aggregate(total=Sum("quantity_returned"))["total"] or Decimal("0")
            available = sale_detail.quantity - already_returned
            quantity_returned = Decimal(str(item["quantity_returned"]))
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
            unit_refund = sale_detail.subtotal / sale_detail.quantity
            subtotal = unit_refund * quantity_returned
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
            },
        )

        return sale_return


class CreditLimitExceededError(APIException):
    status_code = 409
    default_code = "CREDIT_LIMIT_EXCEEDED"

    def __init__(
        self, *, current_debt: Decimal, credit_limit: Decimal, requested: Decimal
    ):
        super().__init__(
            {
                "error": {
                    "code": "CREDIT_LIMIT_EXCEEDED",
                    "message": (
                        f"El cliente supera su limite de credito: deuda actual "
                        f"{current_debt}, limite {credit_limit}, solicitado {requested}."
                    ),
                }
            }
        )


class InsufficientBalanceError(APIException):
    status_code = 409
    default_code = "INSUFFICIENT_BALANCE"

    def __init__(self, *, available: Decimal, requested: Decimal):
        super().__init__(
            {
                "error": {
                    "code": "INSUFFICIENT_BALANCE",
                    "message": (
                        f"El cliente no tiene suficiente saldo a favor: "
                        f"disponible {available}, solicitado {requested}."
                    ),
                }
            }
        )


class CreditLedgerService:
    """Unico punto de entrada a CustomerDebtLedger/CustomerBalanceLedger
    (Sprint 19, Plan de Implementacion): el saldo nunca se calcula ni se
    escribe fuera de aca -ni SaleService.create_sale() ni SaleService.
    void_sale() tocan esos modelos directamente, todos pasan por estos
    metodos. Es lo que faltaba desde el Sprint 18 (ver docstring de
    void_sale): ahora que este servicio existe, la anulacion de una venta a
    credito o pagada con saldo a favor si revierte esos libros."""

    @staticmethod
    def get_debt(customer) -> Decimal:
        totals = CustomerDebtLedger.objects.filter(customer=customer).aggregate(
            debit=Sum("amount", filter=Q(type="DEBIT")),
            credit=Sum("amount", filter=Q(type="CREDIT")),
        )
        return (totals["debit"] or Decimal("0")) - (totals["credit"] or Decimal("0"))

    @staticmethod
    def get_balance(customer) -> Decimal:
        totals = CustomerBalanceLedger.objects.filter(customer=customer).aggregate(
            credit=Sum("amount", filter=Q(type="CREDIT")),
            debit=Sum("amount", filter=Q(type="DEBIT")),
        )
        return (totals["credit"] or Decimal("0")) - (totals["debit"] or Decimal("0"))

    @staticmethod
    def register_credit_sale(
        *, customer, sale: Sale, amount: Decimal
    ) -> CustomerDebtLedger:
        current_debt = CreditLedgerService.get_debt(customer)
        if (
            customer.credit_limit is not None
            and current_debt + amount > customer.credit_limit
        ):
            raise CreditLimitExceededError(
                current_debt=current_debt,
                credit_limit=customer.credit_limit,
                requested=amount,
            )
        return CustomerDebtLedger.objects.create(
            customer=customer,
            sale=sale,
            type="DEBIT",
            amount=amount,
            description=f"Venta a credito {sale.invoice_number}",
        )

    @staticmethod
    def register_balance_use(
        *, customer, sale: Sale, amount: Decimal
    ) -> CustomerBalanceLedger:
        current_balance = CreditLedgerService.get_balance(customer)
        if amount > current_balance:
            raise InsufficientBalanceError(available=current_balance, requested=amount)
        return CustomerBalanceLedger.objects.create(
            customer=customer,
            sale=sale,
            type="DEBIT",
            amount=amount,
            description=f"Pago con saldo a favor, venta {sale.invoice_number}",
        )

    @staticmethod
    def reverse_credit_sale(
        *, customer, sale: Sale, amount: Decimal
    ) -> CustomerDebtLedger:
        """Anulacion de una venta a credito (SaleService.void_sale): el saldo
        adeudado por esa venta se perdona, nunca se borra el DEBIT original
        -mismo principio de "nunca editar/borrar" que el resto del proyecto."""
        return CustomerDebtLedger.objects.create(
            customer=customer,
            sale=sale,
            type="CREDIT",
            amount=amount,
            description=f"Anulacion de venta {sale.invoice_number}",
        )

    @staticmethod
    def reverse_balance_use(
        *, customer, sale: Sale, amount: Decimal
    ) -> CustomerBalanceLedger:
        return CustomerBalanceLedger.objects.create(
            customer=customer,
            sale=sale,
            type="CREDIT",
            amount=amount,
            description=f"Anulacion de venta {sale.invoice_number}",
        )

    @staticmethod
    def register_payment(
        *, customer, amount: Decimal, description: str = ""
    ) -> CustomerDebtLedger:
        return CustomerDebtLedger.objects.create(
            customer=customer,
            type="CREDIT",
            amount=amount,
            description=description or "Abono de fiado",
        )


class SaleSyncService:
    """POST /ventas/sales/sync/ (Sprint 20, API Spec §4.2): procesa el lote
    de ventas que el POS acumulo sin conexion. Idempotente por
    client_side_uuid -reenviar el mismo lote completo (ej. la app reintenta
    porque el request anterior se corto a mitad de la respuesta) nunca
    duplica una venta ni descuenta stock una segunda vez. Cada venta del
    lote es su propia unidad: create_sale() ya es @transaction.atomic por
    venta, asi que una falla en una no revierte ni bloquea las demas."""

    @staticmethod
    def sync_batch(*, sales: list[dict], user) -> dict:
        synced = []
        conflicts = []

        for sale_data in sales:
            client_side_uuid = sale_data["client_side_uuid"]
            existing = Sale.objects.filter(client_side_uuid=client_side_uuid).first()
            if existing is not None:
                synced.append(
                    {
                        "client_side_uuid": client_side_uuid,
                        "status": "DUPLICATE_IGNORED",
                        "sale_id": existing.id,
                    }
                )
                continue

            try:
                sale = SaleService.create_sale(
                    customer=sale_data["customer"],
                    cash_session=sale_data["cash_session"],
                    user=user,
                    lines=sale_data["lines"],
                    payments=sale_data["payments"],
                    client_side_uuid=client_side_uuid,
                    allow_oversell=True,
                )
            except IntegrityError:
                # Dos sincronizaciones casi simultaneas del mismo
                # client_side_uuid (ej. reintento de red del mismo device) -
                # el chequeo de arriba no alcanzo a verla, pero el
                # constraint unico de la tabla si.
                existing = Sale.objects.get(client_side_uuid=client_side_uuid)
                synced.append(
                    {
                        "client_side_uuid": client_side_uuid,
                        "status": "DUPLICATE_IGNORED",
                        "sale_id": existing.id,
                    }
                )
                continue
            except APIException as exc:
                synced.append(
                    {
                        "client_side_uuid": client_side_uuid,
                        "status": "FAILED",
                        "error": exc.detail,
                    }
                )
                continue

            synced.append(
                {
                    "client_side_uuid": client_side_uuid,
                    "status": "CREATED",
                    "sale_id": sale.id,
                }
            )
            for variant_id in sale.oversold_variant_ids:
                conflicts.append(
                    {
                        "client_side_uuid": client_side_uuid,
                        "variant_id": variant_id,
                        "oversell_flag": True,
                    }
                )

        return {"synced": synced, "conflicts": conflicts}
