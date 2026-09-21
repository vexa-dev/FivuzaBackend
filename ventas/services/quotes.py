from decimal import Decimal

from django.db import transaction
from django.template.loader import render_to_string
from django.utils import timezone
from rest_framework.exceptions import APIException

from inventario.models import ProductVariant
from ventas.models import (
    CashSession,
    Quote,
    QuoteDetail,
    Sale,
)
from ventas.services.sales import SaleService


class QuoteNotAcceptedError(APIException):
    status_code = 409
    default_code = "QUOTE_NOT_ACCEPTED"
    default_detail = {
        "error": {
            "code": "QUOTE_NOT_ACCEPTED",
            "message": "Solo se puede convertir una cotizacion aceptada.",
        }
    }


class QuoteAlreadyConvertedError(APIException):
    status_code = 409
    default_code = "QUOTE_ALREADY_CONVERTED"
    default_detail = {
        "error": {
            "code": "QUOTE_ALREADY_CONVERTED",
            "message": "Esta cotizacion ya se convirtio en una venta.",
        }
    }


class QuoteExpiredError(APIException):
    status_code = 409
    default_code = "QUOTE_EXPIRED"
    default_detail = {
        "error": {
            "code": "QUOTE_EXPIRED",
            "message": "Esta cotizacion ya vencio.",
        }
    }


class QuoteService:
    """Cotizacion/presupuesto (Sprint 28, Ficha de Producto §5.2): documento
    no vinculante que congela los precios al momento de cotizar (misma
    resolucion de precio por volumen/promocion que create_sale(), pero
    fijada aqui en vez de recalculada despues). convert_to_sale() reutiliza
    SaleService.create_sale() pasando esos precios ya congelados como
    unit_price/discount_amount explicitos por linea -el total de la venta
    resultante es exactamente el cotizado, sin importar si el precio de
    catalogo o la promocion cambiaron entre medio."""

    @staticmethod
    @transaction.atomic
    def create_quote(
        *, customer, user, lines: list[dict], valid_until, at=None
    ) -> Quote:
        at = at or timezone.now()
        subtotal = Decimal("0")
        discount_total = Decimal("0")
        prepared_lines = []
        for line in lines:
            variant = ProductVariant.objects.select_related("product").get(
                id=line["variant_id"]
            )
            quantity = Decimal(str(line["quantity"]))
            unit_price = SaleService._resolve_unit_price(
                variant=variant, quantity=quantity
            )
            line_subtotal = unit_price * quantity
            discount_amount = line.get("discount_amount")
            discount_amount = (
                Decimal(str(discount_amount))
                if discount_amount is not None
                else SaleService._resolve_promotion_discount(
                    variant=variant, quantity=quantity, unit_price=unit_price, at=at
                )
            )
            prepared_lines.append(
                {
                    "variant": variant,
                    "quantity": quantity,
                    "unit_price": unit_price,
                    "discount_amount": discount_amount,
                    "subtotal": line_subtotal - discount_amount,
                }
            )
            subtotal += line_subtotal
            discount_total += discount_amount

        quote = Quote.objects.create(
            customer=customer,
            user=user,
            valid_until=valid_until,
            subtotal=subtotal,
            discount_total=discount_total,
            total=subtotal - discount_total,
        )
        for prepared in prepared_lines:
            variant = prepared["variant"]
            QuoteDetail.objects.create(
                quote=quote,
                variant_id=variant.id,
                product_name_snapshot=variant.product.name,
                sku_snapshot=variant.sku,
                quantity=prepared["quantity"],
                unit_price=prepared["unit_price"],
                discount_amount=prepared["discount_amount"],
                subtotal=prepared["subtotal"],
            )

        from usuarios.services import AuditLogService

        AuditLogService.log_action(
            user=user,
            action="QUOTE_CREATED",
            entity="Quote",
            entity_id=quote.id,
            details={
                "customer_id": customer.id if customer else None,
                "total": str(quote.total),
                "lines": len(prepared_lines),
                "valid_until": str(valid_until),
            },
        )
        return quote

    @staticmethod
    def mark_sent(*, quote: Quote) -> Quote:
        quote.status = "SENT"
        quote.save(update_fields=["status"])
        return quote

    @staticmethod
    def mark_accepted(*, quote: Quote) -> Quote:
        quote.status = "ACCEPTED"
        quote.save(update_fields=["status"])
        return quote

    @staticmethod
    def mark_rejected(*, quote: Quote) -> Quote:
        quote.status = "REJECTED"
        quote.save(update_fields=["status"])
        return quote

    @staticmethod
    @transaction.atomic
    def convert_to_sale(
        *, quote: Quote, cash_session: CashSession, user, payments: list[dict]
    ) -> Sale:
        if quote.sale_id is not None:
            raise QuoteAlreadyConvertedError()
        if quote.status != "ACCEPTED":
            raise QuoteNotAcceptedError()
        if quote.valid_until < timezone.now():
            raise QuoteExpiredError()

        lines = [
            {
                "variant_id": detail.variant_id,
                "quantity": detail.quantity,
                "unit_price": detail.unit_price,
                "discount_amount": detail.discount_amount,
            }
            for detail in quote.details.all()
        ]
        sale = SaleService.create_sale(
            customer=quote.customer,
            cash_session=cash_session,
            user=user,
            lines=lines,
            payments=payments,
        )
        quote.sale = sale
        quote.save(update_fields=["sale"])

        from usuarios.services import AuditLogService

        AuditLogService.log_action(
            user=user,
            action="QUOTE_CONVERTED",
            entity="Quote",
            entity_id=quote.id,
            details={"sale_id": sale.id},
        )
        return sale

    @staticmethod
    def render_html(quote: Quote, tenant) -> str:
        return render_to_string(
            "ventas/quotes/quote_document.html",
            {
                "company_name": tenant.company_name,
                "ruc": tenant.ruc or "",
                "quote": quote,
                "customer": quote.customer,
                "issued_at": timezone.localtime(quote.created_at).strftime(
                    "%d/%m/%Y %H:%M"
                ),
                "valid_until": timezone.localtime(quote.valid_until).strftime(
                    "%d/%m/%Y"
                ),
                "details": quote.details.all(),
                "subtotal": quote.subtotal.quantize(Decimal("0.01")),
                "discount_total": quote.discount_total.quantize(Decimal("0.01")),
                "total": quote.total.quantize(Decimal("0.01")),
            },
        )
