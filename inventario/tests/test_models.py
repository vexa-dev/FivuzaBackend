# Pruebas de modelos: validaciones de campo, constraints, métodos del modelo.
from django_tenants.test.cases import TenantTestCase

from core.models import TenantSettings
from inventario.models import Category
from inventario.services import ProductVariantService


class ProductVariantServiceTests(TenantTestCase):
    """Invariante: todo producto queda con >=1 variante is_default=True
    (Esquema Backend §5.2, Sprint 3)."""

    @classmethod
    def get_test_schema_name(cls):
        return "test_inventario_service"

    @classmethod
    def get_test_tenant_domain(cls):
        return "test-inventario-service.test.com"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.category = Category.objects.create(name="Ropa")

    @classmethod
    def tearDownClass(cls):
        TenantSettings.objects.filter(tenant=cls.tenant).delete()
        super().tearDownClass()

    def _product_data(self, name="Camiseta"):
        return {
            "type": "PRODUCT",
            "name": name,
            "category": self.category,
            "unit_of_measure": "UND",
        }

    def test_product_without_variants_gets_synthetic_default_variant(self):
        product = ProductVariantService.create_product(
            product_data=self._product_data(), variants_data=[]
        )
        variants = list(product.variants.all())
        self.assertEqual(len(variants), 1)
        self.assertTrue(variants[0].is_default)

    def test_first_variant_becomes_default_when_none_marked(self):
        product = ProductVariantService.create_product(
            product_data=self._product_data("Camiseta con tallas"),
            variants_data=[{"sku": "CAM-S"}, {"sku": "CAM-M"}, {"sku": "CAM-L"}],
        )
        variants = {v.sku: v.is_default for v in product.variants.all()}
        self.assertEqual(len(variants), 3)
        self.assertTrue(variants["CAM-S"])
        self.assertFalse(variants["CAM-M"])
        self.assertFalse(variants["CAM-L"])

    def test_explicit_default_is_respected(self):
        product = ProductVariantService.create_product(
            product_data=self._product_data("Camiseta explicita"),
            variants_data=[
                {"sku": "CAM2-S", "is_default": False},
                {"sku": "CAM2-M", "is_default": True},
            ],
        )
        variants = {v.sku: v.is_default for v in product.variants.all()}
        self.assertFalse(variants["CAM2-S"])
        self.assertTrue(variants["CAM2-M"])

    def test_barcode_is_unique(self):
        ProductVariantService.create_product(
            product_data=self._product_data("Producto A"),
            variants_data=[{"sku": "A-1", "barcode": "7501234567890"}],
        )
        with self.assertRaises(Exception):
            ProductVariantService.create_product(
                product_data=self._product_data("Producto B"),
                variants_data=[{"sku": "B-1", "barcode": "7501234567890"}],
            )


class SoftDeleteModelTests(TenantTestCase):
    """Warehouse/Category/Supplier/Product/ProductVariant heredan
    SoftDeleteModel -un registro dado de baja desaparece del manager
    por defecto (Sprint 2, patron reutilizado en Sprint 3)."""

    @classmethod
    def get_test_schema_name(cls):
        return "test_inventario_softdelete"

    @classmethod
    def get_test_tenant_domain(cls):
        return "test-inventario-softdelete.test.com"

    @classmethod
    def tearDownClass(cls):
        TenantSettings.objects.filter(tenant=cls.tenant).delete()
        super().tearDownClass()

    def test_soft_deleted_category_excluded_from_default_manager(self):
        from django.utils import timezone

        category = Category.objects.create(name="Descontinuada")
        category.deleted_at = timezone.now()
        category.save(update_fields=["deleted_at"])

        self.assertFalse(Category.objects.filter(id=category.id).exists())
        self.assertTrue(Category.all_objects.filter(id=category.id).exists())
