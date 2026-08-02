# Pruebas de modelos: validaciones de campo, constraints, métodos del modelo.
from decimal import Decimal

from django.core.exceptions import ValidationError
from django_tenants.test.cases import TenantTestCase

from core.models import TenantSettings
from inventario.models import (
    Category,
    InventoryMovement,
    ProductPriceHistory,
    PurchaseOrder,
    Stock,
    Supplier,
    Warehouse,
)
from inventario.selectors import get_low_stock_variant_ids
from inventario.services import ProductVariantService, PurchaseService, StockService
from usuarios.models import AuditLog, Role, User


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


class StockServiceTests(TenantTestCase):
    """StockService.adjust_stock(): unico punto de entrada para modificar
    Stock, genera su InventoryMovement en la misma transaccion (Sprint 4)."""

    @classmethod
    def get_test_schema_name(cls):
        return "test_inventario_stock"

    @classmethod
    def get_test_tenant_domain(cls):
        return "test-inventario-stock.test.com"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        role = Role.objects.create(name="admin", is_system_default=True)
        cls.user = User.objects.create(email="admin@negocio.com", role=role)
        cls.warehouse = Warehouse.objects.create(name="Principal")
        cls.category = Category.objects.create(name="Ropa")

    @classmethod
    def tearDownClass(cls):
        TenantSettings.objects.filter(tenant=cls.tenant).delete()
        super().tearDownClass()

    _sku_counter = 0

    def _create_variant(self, min_stock="0"):
        StockServiceTests._sku_counter += 1
        product = ProductVariantService.create_product(
            product_data={
                "type": "PRODUCT",
                "name": "Camiseta",
                "category": self.category,
                "unit_of_measure": "UND",
            },
            variants_data=[{"sku": f"SKU-{StockServiceTests._sku_counter}"}],
        )
        variant = product.variants.first()
        variant.min_stock = min_stock
        variant.save(update_fields=["min_stock"])
        return variant

    def test_first_adjustment_creates_stock_row_and_in_movement(self):
        variant = self._create_variant()
        movement = StockService.adjust_stock(
            variant=variant,
            warehouse=self.warehouse,
            counted_quantity=10,
            concept="ADJUSTMENT",
            user=self.user,
        )
        self.assertEqual(movement.type, "IN")
        self.assertEqual(movement.quantity, 10)
        self.assertEqual(movement.resulting_balance, 10)

        stock = Stock.objects.get(variant=variant, warehouse=self.warehouse)
        self.assertEqual(stock.quantity, 10)

    def test_adjustment_below_current_stock_creates_out_movement(self):
        variant = self._create_variant()
        StockService.adjust_stock(
            variant=variant,
            warehouse=self.warehouse,
            counted_quantity=10,
            concept="ADJUSTMENT",
            user=self.user,
        )
        movement = StockService.adjust_stock(
            variant=variant,
            warehouse=self.warehouse,
            counted_quantity=6,
            concept="ADJUSTMENT",
            user=self.user,
        )
        self.assertEqual(movement.type, "OUT")
        self.assertEqual(movement.quantity, 4)
        self.assertEqual(movement.resulting_balance, 6)

    def test_adjustment_with_no_change_raises(self):
        variant = self._create_variant()
        StockService.adjust_stock(
            variant=variant,
            warehouse=self.warehouse,
            counted_quantity=5,
            concept="ADJUSTMENT",
            user=self.user,
        )
        with self.assertRaises(ValidationError):
            StockService.adjust_stock(
                variant=variant,
                warehouse=self.warehouse,
                counted_quantity=5,
                concept="ADJUSTMENT",
                user=self.user,
            )

    def test_adjustment_writes_audit_log(self):
        variant = self._create_variant()
        StockService.adjust_stock(
            variant=variant,
            warehouse=self.warehouse,
            counted_quantity=3,
            concept="ADJUSTMENT",
            user=self.user,
        )
        self.assertTrue(
            AuditLog.objects.filter(action="STOCK_ADJUSTED", user=self.user).exists()
        )

    def test_low_stock_selector_only_returns_variants_below_min_stock(self):
        low = self._create_variant(min_stock="5")
        healthy = self._create_variant(min_stock="5")
        no_threshold = self._create_variant(min_stock="0")

        StockService.adjust_stock(
            variant=low,
            warehouse=self.warehouse,
            counted_quantity=2,
            concept="ADJUSTMENT",
            user=self.user,
        )
        StockService.adjust_stock(
            variant=healthy,
            warehouse=self.warehouse,
            counted_quantity=20,
            concept="ADJUSTMENT",
            user=self.user,
        )
        # no_threshold nunca se ajusta -min_stock=0 ya lo excluye del
        # selector sin importar su stock real (ver docstring del selector).

        low_stock_ids = set(get_low_stock_variant_ids())
        self.assertIn(low.id, low_stock_ids)
        self.assertNotIn(healthy.id, low_stock_ids)
        self.assertNotIn(no_threshold.id, low_stock_ids)

    def test_low_stock_selector_counts_variant_never_adjusted(self):
        variant = self._create_variant(min_stock="5")
        self.assertIn(variant.id, set(get_low_stock_variant_ids()))

    def test_movement_resulting_balance_matches_final_stock_after_two_adjustments(self):
        variant = self._create_variant()
        StockService.adjust_stock(
            variant=variant,
            warehouse=self.warehouse,
            counted_quantity=10,
            concept="ADJUSTMENT",
            user=self.user,
        )
        StockService.adjust_stock(
            variant=variant,
            warehouse=self.warehouse,
            counted_quantity=25,
            concept="PURCHASE",
            user=self.user,
        )
        stock = Stock.objects.get(variant=variant, warehouse=self.warehouse)
        last_movement = (
            InventoryMovement.objects.filter(variant=variant)
            .order_by("-created_at", "-id")
            .first()
        )
        self.assertEqual(stock.quantity, last_movement.resulting_balance)


class PurchaseServiceTests(TenantTestCase):
    """PurchaseService.receive_order(): recibir una orden mueve Stock via
    StockService y recalcula el costo promedio ponderado (Sprint 5)."""

    @classmethod
    def get_test_schema_name(cls):
        return "test_inventario_purchases"

    @classmethod
    def get_test_tenant_domain(cls):
        return "test-inventario-purchases.test.com"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        role = Role.objects.create(name="admin", is_system_default=True)
        cls.user = User.objects.create(email="admin@negocio.com", role=role)
        cls.warehouse = Warehouse.objects.create(name="Principal")
        cls.supplier = Supplier.objects.create(
            ruc_or_dni="20123456789", company_name="Proveedor SAC"
        )
        category = Category.objects.create(name="Ropa")
        product = ProductVariantService.create_product(
            product_data={
                "type": "PRODUCT",
                "name": "Camiseta",
                "category": category,
                "unit_of_measure": "UND",
            },
            variants_data=[{"sku": "PURCHASE-SKU", "cost": "10.00"}],
        )
        cls.variant = product.variants.first()

    @classmethod
    def tearDownClass(cls):
        TenantSettings.objects.filter(tenant=cls.tenant).delete()
        super().tearDownClass()

    def _create_order(self, quantity="10", unit_cost="12.00"):
        order = PurchaseOrder.objects.create(
            supplier=self.supplier,
            warehouse=self.warehouse,
            user=self.user,
            status="PENDING",
            total="0",
        )
        from inventario.models import PurchaseOrderDetail

        PurchaseOrderDetail.objects.create(
            purchase_order=order,
            variant_id=self.variant.id,
            quantity=quantity,
            unit_cost=unit_cost,
            subtotal=str(float(quantity) * float(unit_cost)),
        )
        return order

    def test_receive_order_creates_stock_and_movement(self):
        order = self._create_order(quantity="10", unit_cost="12.00")
        PurchaseService.receive_order(purchase_order=order, user=self.user)

        stock = Stock.objects.get(variant=self.variant, warehouse=self.warehouse)
        self.assertEqual(stock.quantity, 10)

        movement = InventoryMovement.objects.filter(
            variant=self.variant, concept="PURCHASE"
        ).first()
        self.assertIsNotNone(movement)
        self.assertEqual(movement.type, "IN")

    def test_receive_order_marks_status_received(self):
        order = self._create_order()
        order = PurchaseService.receive_order(purchase_order=order, user=self.user)
        self.assertEqual(order.status, "RECEIVED")
        self.assertIsNotNone(order.received_at)

    def test_receive_order_updates_weighted_average_cost(self):
        # Costo inicial: 10.00 sin stock previo -> tras recibir 10 u. a 12.00,
        # el promedio queda igual a 12.00 (no habia stock previo que pesar).
        order = self._create_order(quantity="10", unit_cost="12.00")
        PurchaseService.receive_order(purchase_order=order, user=self.user)
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.cost, Decimal("12.0000"))

        # Segunda recepcion: 10 u. mas a 20.00 -> promedio ponderado sobre
        # 10@12 + 10@20 = 320/20 = 16.00.
        order2 = self._create_order(quantity="10", unit_cost="20.00")
        PurchaseService.receive_order(purchase_order=order2, user=self.user)
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.cost, Decimal("16.0000"))

        self.assertTrue(
            ProductPriceHistory.objects.filter(variant=self.variant).exists()
        )

    def test_cannot_receive_an_already_received_order(self):
        order = self._create_order()
        PurchaseService.receive_order(purchase_order=order, user=self.user)
        with self.assertRaises(ValidationError):
            PurchaseService.receive_order(purchase_order=order, user=self.user)
