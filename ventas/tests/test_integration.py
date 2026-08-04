# Pruebas de flujo completo a través de la capa de servicios (ej. crear una venta
# de punta a punta), no solo de una unidad aislada.
from datetime import timedelta

from django.utils import timezone
from django_tenants.test.cases import TenantTestCase

from core.models import TenantSettings
from inventario.models import Category
from inventario.services import ProductVariantService
from ventas.models import Promotion, PromotionProduct
from ventas.services import PromotionService


class PromotionServiceTests(TenantTestCase):
    """PromotionService.resolve_active_promotion(): la variante gana sobre la
    categoria, y entre promociones del mismo nivel gana la mas reciente
    (Esquema Backend §6.2, sin una regla de desempate documentada -se asume
    "mas reciente" por ser determinista sin comparar PERCENTAGE vs
    FIXED_AMOUNT)."""

    @classmethod
    def get_test_schema_name(cls):
        return "test_ventas_promotions"

    @classmethod
    def get_test_tenant_domain(cls):
        return "test-ventas-promotions.test.com"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.category = Category.objects.create(name="Ropa")
        cls.other_category = Category.objects.create(name="Calzado")

    @classmethod
    def tearDownClass(cls):
        TenantSettings.objects.filter(tenant=cls.tenant).delete()
        super().tearDownClass()

    _sku_counter = 0

    def _create_variant(self, category=None):
        PromotionServiceTests._sku_counter += 1
        product = ProductVariantService.create_product(
            product_data={
                "type": "PRODUCT",
                "name": "Camiseta",
                "category": category or self.category,
                "unit_of_measure": "UND",
            },
            variants_data=[{"sku": f"SKU-PROMO-{PromotionServiceTests._sku_counter}"}],
        )
        return product.variants.first()

    def _create_promotion(
        self, *, name, start_date=None, end_date=None, is_active=True
    ):
        now = timezone.now()
        return Promotion.objects.create(
            name=name,
            type="PERCENTAGE",
            value="10.00",
            start_date=start_date or now - timedelta(days=1),
            end_date=end_date or now + timedelta(days=1),
            is_active=is_active,
        )

    def test_expired_promotion_does_not_apply(self):
        variant = self._create_variant()
        now = timezone.now()
        expired = self._create_promotion(
            name="Vencida",
            start_date=now - timedelta(days=10),
            end_date=now - timedelta(days=1),
        )
        PromotionProduct.objects.create(promotion=expired, variant=variant)

        result = PromotionService.resolve_active_promotion(variant=variant, at=now)
        self.assertIsNone(result)

    def test_future_promotion_does_not_apply(self):
        variant = self._create_variant()
        now = timezone.now()
        future = self._create_promotion(
            name="Futura",
            start_date=now + timedelta(days=1),
            end_date=now + timedelta(days=10),
        )
        PromotionProduct.objects.create(promotion=future, variant=variant)

        result = PromotionService.resolve_active_promotion(variant=variant, at=now)
        self.assertIsNone(result)

    def test_variant_targeted_promotion_wins_over_category(self):
        variant = self._create_variant()
        category_promo = self._create_promotion(name="Toda la categoria")
        PromotionProduct.objects.create(
            promotion=category_promo, category=self.category
        )
        variant_promo = self._create_promotion(name="Solo esta variante")
        PromotionProduct.objects.create(promotion=variant_promo, variant=variant)

        result = PromotionService.resolve_active_promotion(variant=variant)
        self.assertEqual(result.id, variant_promo.id)

    def test_tie_break_is_deterministic_most_recent_wins(self):
        variant = self._create_variant()
        older = self._create_promotion(name="Promo A")
        PromotionProduct.objects.create(promotion=older, category=self.category)
        newer = self._create_promotion(name="Promo B")
        PromotionProduct.objects.create(promotion=newer, category=self.category)

        result = PromotionService.resolve_active_promotion(variant=variant)
        self.assertEqual(result.id, newer.id)

    def test_promotion_for_other_category_does_not_apply(self):
        variant = self._create_variant()
        other_promo = self._create_promotion(name="Solo calzado")
        PromotionProduct.objects.create(
            promotion=other_promo, category=self.other_category
        )

        result = PromotionService.resolve_active_promotion(variant=variant)
        self.assertIsNone(result)

    def test_inactive_promotion_does_not_apply(self):
        variant = self._create_variant()
        inactive = self._create_promotion(name="Desactivada", is_active=False)
        PromotionProduct.objects.create(promotion=inactive, variant=variant)

        result = PromotionService.resolve_active_promotion(variant=variant)
        self.assertIsNone(result)
