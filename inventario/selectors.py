from decimal import Decimal

from django.db.models import F, Q, Sum
from django.db.models.functions import Coalesce

from inventario.models import ProductVariant


def get_low_stock_variants(warehouse_ids=None):
    """Variantes cuyo stock total (sumado entre almacenes) esta por debajo
    de su min_stock. min_stock=0 (el default) significa "sin umbral
    configurado" -esas variantes nunca aparecen aqui, a proposito: no se
    puede alertar sobre un limite que el negocio no definio."""
    if warehouse_ids is not None and not warehouse_ids:
        return ProductVariant.objects.none()
    stock_filter = (
        Q(stock__warehouse_id__in=warehouse_ids) if warehouse_ids is not None else Q()
    )
    return (
        ProductVariant.objects.filter(is_active=True, min_stock__gt=0)
        .select_related("product")
        .annotate(
            total_stock=Coalesce(
                Sum("stock__quantity", filter=stock_filter), Decimal("0")
            )
        )
        .filter(total_stock__lt=F("min_stock"))
        .order_by("total_stock")
    )


def get_low_stock_variant_ids(warehouse_ids=None):
    return get_low_stock_variants(warehouse_ids).values_list("id", flat=True)
