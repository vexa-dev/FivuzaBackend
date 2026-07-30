from decimal import Decimal

from django.db.models import F, Sum
from django.db.models.functions import Coalesce

from inventario.models import ProductVariant


def get_low_stock_variants():
    """Variantes cuyo stock total (sumado entre almacenes) esta por debajo
    de su min_stock. min_stock=0 (el default) significa "sin umbral
    configurado" -esas variantes nunca aparecen aqui, a proposito: no se
    puede alertar sobre un limite que el negocio no definio."""
    return (
        ProductVariant.objects.filter(is_active=True, min_stock__gt=0)
        .select_related("product")
        .annotate(total_stock=Coalesce(Sum("stock__quantity"), Decimal("0")))
        .filter(total_stock__lt=F("min_stock"))
        .order_by("total_stock")
    )


def get_low_stock_variant_ids():
    return get_low_stock_variants().values_list("id", flat=True)
