from decimal import Decimal

from django.db.models import Q

from inventario.models import ProductVariant, Stock
from ventas.services.promotions import PromotionService


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
        from inventario.models import VolumePricingTier

        variant_ids = [variant.id for variant in variants]
        stock_by_variant = dict(
            Stock.objects.filter(
                warehouse=warehouse, variant_id__in=variant_ids
            ).values_list("variant_id", "quantity")
        )
        by_variant, by_category = PromotionService.build_active_promotion_index()

        # Sprint 26: el POS necesita los tramos para mostrar "precio
        # mayorista aplicado" en vivo, sin round-trip al agregar unidades
        # al carrito -mismo criterio que el resto del catalogo (payload
        # completo, pensado para cachearse).
        tiers_by_variant: dict[int, list[dict]] = {}
        for tier in VolumePricingTier.objects.filter(
            variant_id__in=variant_ids
        ).order_by("variant_id", "min_quantity"):
            tiers_by_variant.setdefault(tier.variant_id, []).append(
                {
                    "min_quantity": str(tier.min_quantity),
                    "unit_price": str(tier.unit_price),
                }
            )

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
                    # Sprint 27: el POS necesita saber si el producto se
                    # vende por peso (KG) para activar la lectura de
                    # balanza en la cantidad -mismo campo que ya trae
                    # Product.unit_of_measure, sin transformar.
                    "unit_of_measure": variant.product.unit_of_measure,
                    "price": str(variant.price),
                    "stock": str(stock_by_variant.get(variant.id, Decimal("0"))),
                    "pricing_tiers": tiers_by_variant.get(variant.id, []),
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
