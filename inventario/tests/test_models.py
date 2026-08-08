# Pruebas de modelos: validaciones de campo, constraints, métodos del modelo.
from decimal import Decimal

from django.core.exceptions import ValidationError
from django_tenants.test.cases import TenantTestCase

from core.models import TenantSettings
from inventario.models import (
    Category,
    InventoryMovement,
    ProductPriceHistory,
    ProductVariant,
    PurchaseOrder,
    Stock,
    Supplier,
    Warehouse,
)
from inventario.selectors import get_low_stock_variant_ids
from inventario.services import (
    CatalogImportService,
    LabelService,
    ProductVariantService,
    PurchaseService,
    StockService,
)
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


class StockServiceTransferTests(TenantTestCase):
    """StockService.transfer_stock() (Sprint 26, Ficha de Producto §5.1):
    dos llamadas a adjust_stock() (TRANSFER_OUT/TRANSFER_IN) dentro de la
    misma transaccion, vinculadas entre si via reference_id."""

    @classmethod
    def get_test_schema_name(cls):
        return "test_inventario_transfer"

    @classmethod
    def get_test_tenant_domain(cls):
        return "test-inventario-transfer.test.com"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        role = Role.objects.create(name="admin", is_system_default=True)
        cls.user = User.objects.create(email="admin@negocio.com", role=role)
        cls.origin = Warehouse.objects.create(name="Principal")
        cls.destination = Warehouse.objects.create(name="Sucursal")
        cls.category = Category.objects.create(name="Ropa")

    @classmethod
    def tearDownClass(cls):
        TenantSettings.objects.filter(tenant=cls.tenant).delete()
        super().tearDownClass()

    _sku_counter = 0

    def _create_variant(self):
        StockServiceTransferTests._sku_counter += 1
        product = ProductVariantService.create_product(
            product_data={
                "type": "PRODUCT",
                "name": "Camiseta",
                "category": self.category,
                "unit_of_measure": "UND",
            },
            variants_data=[
                {"sku": f"SKU-TRANSFER-{StockServiceTransferTests._sku_counter}"}
            ],
        )
        return product.variants.first()

    def test_transfer_moves_stock_between_warehouses(self):
        variant = self._create_variant()
        StockService.adjust_stock(
            variant=variant,
            warehouse=self.origin,
            counted_quantity=10,
            concept="ADJUSTMENT",
            user=self.user,
        )

        out_movement, in_movement = StockService.transfer_stock(
            variant=variant,
            from_warehouse=self.origin,
            to_warehouse=self.destination,
            quantity=Decimal("4"),
            user=self.user,
        )

        self.assertEqual(out_movement.concept, "TRANSFER_OUT")
        self.assertEqual(out_movement.type, "OUT")
        self.assertEqual(out_movement.resulting_balance, 6)
        self.assertEqual(in_movement.concept, "TRANSFER_IN")
        self.assertEqual(in_movement.type, "IN")
        self.assertEqual(in_movement.resulting_balance, 4)
        self.assertEqual(out_movement.reference_id, in_movement.id)
        self.assertEqual(in_movement.reference_id, out_movement.id)

        origin_stock = Stock.objects.get(variant=variant, warehouse=self.origin)
        dest_stock = Stock.objects.get(variant=variant, warehouse=self.destination)
        self.assertEqual(origin_stock.quantity, 6)
        self.assertEqual(dest_stock.quantity, 4)

    def test_transfer_cannot_exceed_available_stock_in_origin(self):
        variant = self._create_variant()
        StockService.adjust_stock(
            variant=variant,
            warehouse=self.origin,
            counted_quantity=3,
            concept="ADJUSTMENT",
            user=self.user,
        )

        with self.assertRaises(ValidationError):
            StockService.transfer_stock(
                variant=variant,
                from_warehouse=self.origin,
                to_warehouse=self.destination,
                quantity=Decimal("5"),
                user=self.user,
            )

        origin_stock = Stock.objects.get(variant=variant, warehouse=self.origin)
        self.assertEqual(origin_stock.quantity, 3)
        self.assertFalse(
            Stock.objects.filter(variant=variant, warehouse=self.destination).exists()
        )

    def test_transfer_to_same_warehouse_is_rejected(self):
        variant = self._create_variant()
        StockService.adjust_stock(
            variant=variant,
            warehouse=self.origin,
            counted_quantity=10,
            concept="ADJUSTMENT",
            user=self.user,
        )

        with self.assertRaises(ValidationError):
            StockService.transfer_stock(
                variant=variant,
                from_warehouse=self.origin,
                to_warehouse=self.origin,
                quantity=Decimal("1"),
                user=self.user,
            )

    def test_transfer_into_new_warehouse_creates_stock_row(self):
        variant = self._create_variant()
        StockService.adjust_stock(
            variant=variant,
            warehouse=self.origin,
            counted_quantity=10,
            concept="ADJUSTMENT",
            user=self.user,
        )

        StockService.transfer_stock(
            variant=variant,
            from_warehouse=self.origin,
            to_warehouse=self.destination,
            quantity=Decimal("10"),
            user=self.user,
        )

        dest_stock = Stock.objects.get(variant=variant, warehouse=self.destination)
        self.assertEqual(dest_stock.quantity, 10)


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


class CatalogImportServiceTests(TenantTestCase):
    """Importacion masiva de catalogo desde CSV (Sprint 6, hueco #3)."""

    @classmethod
    def get_test_schema_name(cls):
        return "test_inventario_catalog_import"

    @classmethod
    def get_test_tenant_domain(cls):
        return "test-inventario-catalog-import.test.com"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        role = Role.objects.create(name="admin", is_system_default=True)
        cls.user = User.objects.create(email="admin@negocio.com", role=role)
        # "Principal" ya existe por defecto (Sprint 12,
        # TenantProvisioningService.seed_default_resources) -crearlo de
        # nuevo aqui duplicaria el nombre y Warehouse.objects.get(name__iexact=...)
        # en CatalogImportService reventaria con MultipleObjectsReturned.
        cls.warehouse = Warehouse.objects.get(name="Principal")
        Category.objects.create(name="Ropa")

    @classmethod
    def tearDownClass(cls):
        TenantSettings.objects.filter(tenant=cls.tenant).delete()
        super().tearDownClass()

    _HEADER = (
        "nombre_producto,categoria,sku,codigo_barras,costo,precio,"
        "stock_minimo,cantidad_inicial,almacen"
    )

    def test_valid_row_creates_product_variant_and_stock(self):
        csv_content = (
            f"{self._HEADER}\n"
            "Camiseta basica,Ropa,CAM-IMP-1,7501234567890,10.00,25.90,5,20,Principal\n"
        )
        report = CatalogImportService.import_csv(
            file_content=csv_content, user=self.user
        )

        self.assertEqual(report["total"], 1)
        self.assertEqual(report["created"], 1)
        self.assertEqual(report["errors"], 0)

        variant = ProductVariant.objects.get(sku="CAM-IMP-1")
        self.assertEqual(variant.barcode, "7501234567890")
        self.assertEqual(variant.price, Decimal("25.9000"))

        stock = Stock.objects.get(variant=variant, warehouse=self.warehouse)
        self.assertEqual(stock.quantity, 20)

    def test_duplicate_barcode_within_file_reports_error_without_blocking_others(self):
        csv_content = (
            f"{self._HEADER}\n"
            "Producto 1,Ropa,SKU-DUP-1,1111111111111,5,10,0,0,\n"
            "Producto 2,Ropa,SKU-DUP-2,1111111111111,5,10,0,0,\n"
            "Producto 3,Ropa,SKU-DUP-3,,5,10,0,0,\n"
        )
        report = CatalogImportService.import_csv(
            file_content=csv_content, user=self.user
        )

        self.assertEqual(report["total"], 3)
        self.assertEqual(report["created"], 2)
        self.assertEqual(report["errors"], 1)
        error_row = next(r for r in report["rows"] if r["status"] == "error")
        self.assertIn("duplicado", error_row["error"])
        self.assertTrue(ProductVariant.objects.filter(sku="SKU-DUP-1").exists())
        self.assertFalse(ProductVariant.objects.filter(sku="SKU-DUP-2").exists())
        self.assertTrue(ProductVariant.objects.filter(sku="SKU-DUP-3").exists())

    def test_duplicate_barcode_against_existing_catalog_is_rejected(self):
        ProductVariantService.create_product(
            product_data={
                "type": "PRODUCT",
                "name": "Existente",
                "category": Category.objects.get(name="Ropa"),
                "unit_of_measure": "UND",
            },
            variants_data=[{"sku": "SKU-EXISTENTE", "barcode": "9999999999999"}],
        )
        csv_content = (
            f"{self._HEADER}\nProducto nuevo,Ropa,SKU-NUEVO,9999999999999,5,10,0,0,\n"
        )
        report = CatalogImportService.import_csv(
            file_content=csv_content, user=self.user
        )
        self.assertEqual(report["created"], 0)
        self.assertEqual(report["errors"], 1)
        self.assertIn("ya existe", report["rows"][0]["error"])

    def test_unknown_category_is_rejected(self):
        csv_content = (
            f"{self._HEADER}\nProducto,Categoria Inexistente,SKU-CAT-X,,5,10,0,0,\n"
        )
        report = CatalogImportService.import_csv(
            file_content=csv_content, user=self.user
        )
        self.assertEqual(report["errors"], 1)
        self.assertIn("categoria", report["rows"][0]["error"])

    def test_initial_quantity_without_warehouse_is_rejected(self):
        csv_content = f"{self._HEADER}\nProducto,Ropa,SKU-NO-ALMACEN,,5,10,0,10,\n"
        report = CatalogImportService.import_csv(
            file_content=csv_content, user=self.user
        )
        self.assertEqual(report["errors"], 1)
        self.assertIn("almacen", report["rows"][0]["error"])

    def test_missing_columns_raises(self):
        with self.assertRaises(ValidationError):
            CatalogImportService.import_csv(
                file_content="nombre_producto,sku\nX,Y\n", user=self.user
            )

    def test_template_csv_has_expected_headers(self):
        template = CatalogImportService.build_template_csv()
        first_line = template.splitlines()[0]
        self.assertIn("nombre_producto", first_line)
        self.assertIn("codigo_barras", first_line)


class LabelServiceTests(TenantTestCase):
    """LabelService: imagen del código de barras (SVG/PNG) y HTML
    imprimible de una hoja de etiquetas (Sprint 27, Ficha de Producto §5.1)."""

    @classmethod
    def get_test_schema_name(cls):
        return "test_inventario_labels"

    @classmethod
    def get_test_tenant_domain(cls):
        return "test-inventario-labels.test.com"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.category = Category.objects.create(name="Ropa")

    @classmethod
    def tearDownClass(cls):
        TenantSettings.objects.filter(tenant=cls.tenant).delete()
        super().tearDownClass()

    def _create_variant(self, *, barcode="7501234567890", price="29.90"):
        product = ProductVariantService.create_product(
            product_data={
                "type": "PRODUCT",
                "name": "Camiseta",
                "category": self.category,
                "unit_of_measure": "UND",
            },
            variants_data=[{"sku": "SKU-LABEL", "barcode": barcode, "price": price}],
        )
        return product.variants.first()

    def test_generate_barcode_svg_returns_svg_markup(self):
        variant = self._create_variant()
        svg = LabelService.generate_barcode_svg(variant)
        self.assertIn("<svg", svg)

    def test_generate_barcode_png_returns_png_bytes(self):
        variant = self._create_variant()
        png = LabelService.generate_barcode_png(variant)
        self.assertTrue(png.startswith(b"\x89PNG"))

    def test_generate_barcode_without_assigned_barcode_raises(self):
        variant = self._create_variant(barcode=None)
        with self.assertRaises(ValidationError):
            LabelService.generate_barcode_svg(variant)

    def test_render_labels_html_repeats_label_per_quantity(self):
        variant = self._create_variant(price="15.50")
        html = LabelService.render_labels_html(
            [{"variant": variant, "quantity": 3}], size="40x25"
        )
        self.assertEqual(html.count("label-price"), 3)
        self.assertIn("15.50", html)
        self.assertIn("width:40mm", html)

    def test_render_labels_html_rejects_unsupported_size(self):
        variant = self._create_variant()
        with self.assertRaises(ValidationError):
            LabelService.render_labels_html(
                [{"variant": variant, "quantity": 1}], size="99x99"
            )
