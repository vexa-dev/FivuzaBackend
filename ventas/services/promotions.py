from django.utils import timezone

from ventas.models import (
    Promotion,
)


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
