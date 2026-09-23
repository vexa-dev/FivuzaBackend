from decimal import ROUND_HALF_UP, Decimal

from django.db import migrations

CENT = Decimal("0.01")


def _round(value: Decimal) -> Decimal:
    # Copia de ventas.services.sales.round_money: una migracion no debe
    # importar codigo de la app, que puede cambiar despues.
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def round_pending_quote_amounts(apps, schema_editor):
    """Las cotizaciones sin convertir se crearon antes de redondear a
    centimos. convert_to_sale() le pasa sus montos congelados a
    create_sale(), que ahora los redondea: sin esto el total de la venta
    diferiria del cotizado y el pago precargado (quote.total) rebotaria con
    PAYMENT_MISMATCH. Misma cuenta que create_sale(), tope incluido. Las
    ventas y las cotizaciones ya convertidas no se tocan."""
    Quote = apps.get_model("ventas", "Quote")
    QuoteDetail = apps.get_model("ventas", "QuoteDetail")

    for quote in Quote.objects.filter(sale__isnull=True).iterator():
        subtotal = Decimal("0")
        discount_total = Decimal("0")
        details = list(QuoteDetail.objects.filter(quote=quote))
        for detail in details:
            line_subtotal = _round(detail.unit_price * detail.quantity)
            detail.discount_amount = _round(min(detail.discount_amount, line_subtotal))
            detail.subtotal = line_subtotal - detail.discount_amount
            subtotal += line_subtotal
            discount_total += detail.discount_amount
        QuoteDetail.objects.bulk_update(details, ["discount_amount", "subtotal"])

        quote.subtotal = subtotal
        quote.discount_total = discount_total
        quote.total = subtotal - discount_total
        quote.save(update_fields=["subtotal", "discount_total", "total"])


class Migration(migrations.Migration):
    dependencies = [
        ("ventas", "0010_cash_session_pending_approval"),
    ]

    operations = [
        migrations.RunPython(round_pending_quote_amounts, migrations.RunPython.noop),
    ]
