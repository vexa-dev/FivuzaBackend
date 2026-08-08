# Pruebas de flujo completo a través de la capa de servicios (ej. crear una venta
# de punta a punta), no solo de una unidad aislada.
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone
from django_tenants.test.cases import TenantTestCase

from core.models import TenantSettings
from inventario.models import Category, Stock, VolumePricingTier, Warehouse
from inventario.services import ProductVariantService, StockService
from usuarios.models import AuditLog, Role, User
from ventas.models import (
    CashRegister,
    CashSession,
    Customer,
    Promotion,
    PromotionProduct,
    Sale,
)
from ventas.services import (
    InsufficientStockError,
    NoCashSessionError,
    PaymentMismatchError,
    POSCatalogService,
    PromotionService,
    SaleService,
)


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


class SaleServiceTests(TenantTestCase):
    """SaleService.create_sale(): el servicio con mayor superficie de riesgo
    del proyecto (TRD §7.2). Cubre los 6 escenarios que el Plan de
    Implementacion pide explicitamente para el Sprint 15."""

    @classmethod
    def get_test_schema_name(cls):
        return "test_ventas_sale_service"

    @classmethod
    def get_test_tenant_domain(cls):
        return "test-ventas-sale-service.test.com"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        role = Role.objects.get(name="admin")
        cls.user = User.objects.create(email="admin@negocio.com", role=role)
        cls.warehouse = Warehouse.objects.create(name="Principal")
        cls.category = Category.objects.create(name="Ropa")
        cls.customer = Customer.objects.create(
            document_type="DNI", document_number="11111111", name="Cliente Uno"
        )

    @classmethod
    def tearDownClass(cls):
        TenantSettings.objects.filter(tenant=cls.tenant).delete()
        super().tearDownClass()

    _sku_counter = 0

    def _create_variant(self, *, price="20.00", stock_quantity="10"):
        SaleServiceTests._sku_counter += 1
        product = ProductVariantService.create_product(
            product_data={
                "type": "PRODUCT",
                "name": "Camiseta",
                "category": self.category,
                "unit_of_measure": "UND",
            },
            variants_data=[
                {"sku": f"SKU-SALE-{SaleServiceTests._sku_counter}", "price": price}
            ],
        )
        variant = product.variants.first()
        StockService.adjust_stock(
            variant=variant,
            warehouse=self.warehouse,
            counted_quantity=Decimal(stock_quantity),
            concept="ADJUSTMENT",
            user=self.user,
        )
        return variant

    def _open_session(self):
        SaleServiceTests._sku_counter += 1
        register = CashRegister.objects.create(
            warehouse=self.warehouse, name=f"Caja {SaleServiceTests._sku_counter}"
        )
        return CashSession.objects.create(
            cash_register=register,
            user=self.user,
            opening_amount="0",
            opening_at=timezone.now(),
            status="OPEN",
        )

    def test_creates_simple_sale_and_discounts_stock(self):
        variant = self._create_variant(price="20.00", stock_quantity="10")
        session = self._open_session()

        sale = SaleService.create_sale(
            customer=self.customer,
            cash_session=session,
            user=self.user,
            lines=[{"variant_id": variant.id, "quantity": "3"}],
            payments=[{"method": "CASH", "amount": Decimal("60.00")}],
        )

        self.assertEqual(sale.status, "COMPLETED")
        self.assertEqual(sale.payment_status, "PAID")
        self.assertEqual(sale.subtotal, Decimal("60.0000"))
        self.assertEqual(sale.total, Decimal("60.0000"))
        self.assertEqual(sale.details.count(), 1)
        self.assertEqual(sale.payments.count(), 1)

        stock = Stock.objects.get(variant=variant, warehouse=self.warehouse)
        self.assertEqual(stock.quantity, Decimal("7.000"))

    def test_creates_sale_with_multiple_payments(self):
        variant = self._create_variant(price="20.00", stock_quantity="10")
        session = self._open_session()

        sale = SaleService.create_sale(
            customer=self.customer,
            cash_session=session,
            user=self.user,
            lines=[{"variant_id": variant.id, "quantity": "2"}],
            payments=[
                {"method": "CASH", "amount": Decimal("25.00")},
                {"method": "CARD", "amount": Decimal("15.00")},
            ],
        )

        self.assertEqual(sale.total, Decimal("40.0000"))
        self.assertEqual(sale.payments.count(), 2)

    def test_creates_sale_applies_active_promotion(self):
        variant = self._create_variant(price="100.00", stock_quantity="10")
        promotion = Promotion.objects.create(
            name="20% descuento",
            type="PERCENTAGE",
            value="20.00",
            start_date=timezone.now() - timedelta(days=1),
            end_date=timezone.now() + timedelta(days=1),
            is_active=True,
        )
        PromotionProduct.objects.create(promotion=promotion, variant=variant)
        session = self._open_session()

        sale = SaleService.create_sale(
            customer=self.customer,
            cash_session=session,
            user=self.user,
            lines=[{"variant_id": variant.id, "quantity": "1"}],
            payments=[{"method": "CASH", "amount": Decimal("80.00")}],
        )

        self.assertEqual(sale.discount_total, Decimal("20.0000"))
        self.assertEqual(sale.total, Decimal("80.0000"))

    def test_manual_discount_overrides_promotion(self):
        variant = self._create_variant(price="100.00", stock_quantity="10")
        promotion = Promotion.objects.create(
            name="20% descuento",
            type="PERCENTAGE",
            value="20.00",
            start_date=timezone.now() - timedelta(days=1),
            end_date=timezone.now() + timedelta(days=1),
            is_active=True,
        )
        PromotionProduct.objects.create(promotion=promotion, variant=variant)
        session = self._open_session()

        sale = SaleService.create_sale(
            customer=self.customer,
            cash_session=session,
            user=self.user,
            lines=[
                {
                    "variant_id": variant.id,
                    "quantity": "1",
                    "discount_amount": Decimal("5.00"),
                }
            ],
            payments=[{"method": "CASH", "amount": Decimal("95.00")}],
        )

        self.assertEqual(sale.discount_total, Decimal("5.00"))
        self.assertEqual(sale.total, Decimal("95.00"))

    def test_insufficient_stock_rolls_back_everything(self):
        variant = self._create_variant(price="20.00", stock_quantity="2")
        session = self._open_session()
        sales_before = Sale.objects.count()

        with self.assertRaises(InsufficientStockError):
            SaleService.create_sale(
                customer=self.customer,
                cash_session=session,
                user=self.user,
                lines=[{"variant_id": variant.id, "quantity": "5"}],
                payments=[{"method": "CASH", "amount": Decimal("100.00")}],
            )

        self.assertEqual(Sale.objects.count(), sales_before)
        stock = Stock.objects.get(variant=variant, warehouse=self.warehouse)
        self.assertEqual(stock.quantity, Decimal("2.000"))

    def test_payment_mismatch_rolls_back_everything(self):
        variant = self._create_variant(price="20.00", stock_quantity="10")
        session = self._open_session()
        sales_before = Sale.objects.count()

        with self.assertRaises(PaymentMismatchError):
            SaleService.create_sale(
                customer=self.customer,
                cash_session=session,
                user=self.user,
                lines=[{"variant_id": variant.id, "quantity": "2"}],
                payments=[{"method": "CASH", "amount": Decimal("10.00")}],
            )

        self.assertEqual(Sale.objects.count(), sales_before)
        stock = Stock.objects.get(variant=variant, warehouse=self.warehouse)
        self.assertEqual(stock.quantity, Decimal("10.000"))

    def test_no_open_cash_session_raises(self):
        variant = self._create_variant(price="20.00", stock_quantity="10")
        session = self._open_session()
        session.status = "CLOSED"
        session.save(update_fields=["status"])

        with self.assertRaises(NoCashSessionError):
            SaleService.create_sale(
                customer=self.customer,
                cash_session=session,
                user=self.user,
                lines=[{"variant_id": variant.id, "quantity": "1"}],
                payments=[{"method": "CASH", "amount": Decimal("20.00")}],
            )

    def test_sale_writes_audit_log_and_unique_invoice_numbers(self):
        variant = self._create_variant(price="20.00", stock_quantity="10")
        session_a = self._open_session()
        session_b = self._open_session()

        sale_a = SaleService.create_sale(
            customer=self.customer,
            cash_session=session_a,
            user=self.user,
            lines=[{"variant_id": variant.id, "quantity": "1"}],
            payments=[{"method": "CASH", "amount": Decimal("20.00")}],
        )
        sale_b = SaleService.create_sale(
            customer=self.customer,
            cash_session=session_b,
            user=self.user,
            lines=[{"variant_id": variant.id, "quantity": "1"}],
            payments=[{"method": "CASH", "amount": Decimal("20.00")}],
        )

        self.assertNotEqual(sale_a.invoice_number, sale_b.invoice_number)
        self.assertTrue(
            AuditLog.objects.filter(action="SALE_CREATED", entity_id=sale_a.id).exists()
        )


class SaleServiceVolumePricingTests(TenantTestCase):
    """SaleService._resolve_unit_price(): resuelve el tramo de mayor
    min_quantity que la cantidad vendida alcance a cubrir (Sprint 26, BDD
    v5 "volume_pricing_tiers"); si ninguno aplica, usa
    ProductVariant.price sin cambios. Corre antes de las promociones, asi
    que una promocion sigue pudiendo aplicar un descuento adicional sobre
    el precio mayorista ya resuelto."""

    @classmethod
    def get_test_schema_name(cls):
        return "test_ventas_volume_pricing"

    @classmethod
    def get_test_tenant_domain(cls):
        return "test-ventas-volume-pricing.test.com"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        role = Role.objects.get(name="admin")
        cls.user = User.objects.create(email="admin@negocio.com", role=role)
        cls.warehouse = Warehouse.objects.create(name="Principal")
        cls.category = Category.objects.create(name="Ropa")
        cls.customer = Customer.objects.create(
            document_type="DNI", document_number="22222222", name="Cliente Dos"
        )

    @classmethod
    def tearDownClass(cls):
        TenantSettings.objects.filter(tenant=cls.tenant).delete()
        super().tearDownClass()

    _sku_counter = 0

    def _create_variant(self, *, price="20.00", stock_quantity="100"):
        SaleServiceVolumePricingTests._sku_counter += 1
        product = ProductVariantService.create_product(
            product_data={
                "type": "PRODUCT",
                "name": "Camiseta",
                "category": self.category,
                "unit_of_measure": "UND",
            },
            variants_data=[
                {
                    "sku": f"SKU-VOLPRICE-{SaleServiceVolumePricingTests._sku_counter}",
                    "price": price,
                }
            ],
        )
        variant = product.variants.first()
        StockService.adjust_stock(
            variant=variant,
            warehouse=self.warehouse,
            counted_quantity=Decimal(stock_quantity),
            concept="ADJUSTMENT",
            user=self.user,
        )
        return variant

    def _open_session(self):
        SaleServiceVolumePricingTests._sku_counter += 1
        register = CashRegister.objects.create(
            warehouse=self.warehouse,
            name=f"Caja {SaleServiceVolumePricingTests._sku_counter}",
        )
        return CashSession.objects.create(
            cash_register=register,
            user=self.user,
            opening_amount="0",
            opening_at=timezone.now(),
            status="OPEN",
        )

    def test_price_unchanged_when_quantity_does_not_reach_threshold(self):
        variant = self._create_variant(price="20.00")
        VolumePricingTier.objects.create(
            variant=variant, min_quantity="12", unit_price="15.00"
        )
        session = self._open_session()

        sale = SaleService.create_sale(
            customer=self.customer,
            cash_session=session,
            user=self.user,
            lines=[{"variant_id": variant.id, "quantity": "5"}],
            payments=[{"method": "CASH", "amount": Decimal("100.00")}],
        )

        self.assertEqual(sale.details.first().unit_price, Decimal("20.00"))
        self.assertEqual(sale.total, Decimal("100.00"))

    def test_tier_price_applies_exactly_at_threshold(self):
        variant = self._create_variant(price="20.00")
        VolumePricingTier.objects.create(
            variant=variant, min_quantity="12", unit_price="15.00"
        )
        session = self._open_session()

        sale = SaleService.create_sale(
            customer=self.customer,
            cash_session=session,
            user=self.user,
            lines=[{"variant_id": variant.id, "quantity": "12"}],
            payments=[{"method": "CASH", "amount": Decimal("180.00")}],
        )

        self.assertEqual(sale.details.first().unit_price, Decimal("15.00"))
        self.assertEqual(sale.total, Decimal("180.00"))

    def test_most_specific_tier_wins_when_quantity_crosses_several(self):
        variant = self._create_variant(price="20.00")
        VolumePricingTier.objects.create(
            variant=variant, min_quantity="12", unit_price="15.00"
        )
        VolumePricingTier.objects.create(
            variant=variant, min_quantity="24", unit_price="12.00"
        )
        session = self._open_session()

        sale = SaleService.create_sale(
            customer=self.customer,
            cash_session=session,
            user=self.user,
            lines=[{"variant_id": variant.id, "quantity": "30"}],
            payments=[{"method": "CASH", "amount": Decimal("360.00")}],
        )

        self.assertEqual(sale.details.first().unit_price, Decimal("12.00"))
        self.assertEqual(sale.total, Decimal("360.00"))

    def test_promotion_still_applies_on_top_of_tier_price(self):
        variant = self._create_variant(price="20.00")
        VolumePricingTier.objects.create(
            variant=variant, min_quantity="12", unit_price="10.00"
        )
        promotion = Promotion.objects.create(
            name="10% descuento",
            type="PERCENTAGE",
            value="10.00",
            start_date=timezone.now() - timedelta(days=1),
            end_date=timezone.now() + timedelta(days=1),
            is_active=True,
        )
        PromotionProduct.objects.create(promotion=promotion, variant=variant)
        session = self._open_session()

        sale = SaleService.create_sale(
            customer=self.customer,
            cash_session=session,
            user=self.user,
            lines=[{"variant_id": variant.id, "quantity": "12"}],
            payments=[{"method": "CASH", "amount": Decimal("108.00")}],
        )

        detail = sale.details.first()
        self.assertEqual(detail.unit_price, Decimal("10.00"))
        self.assertEqual(sale.discount_total, Decimal("12.0000"))
        self.assertEqual(sale.total, Decimal("108.00"))


class POSCatalogServiceTests(TenantTestCase):
    """POSCatalogService.search()/catalog() (Sprint 16, Esquema Backend
    §6.2): prioridad de escaneo por barcode, fallback difuso por nombre/sku,
    stock por almacen y promocion vigente en el mismo payload."""

    @classmethod
    def get_test_schema_name(cls):
        return "test_ventas_pos_catalog"

    @classmethod
    def get_test_tenant_domain(cls):
        return "test-ventas-pos-catalog.test.com"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        role = Role.objects.get(name="admin")
        cls.user = User.objects.create(email="admin@negocio.com", role=role)
        cls.warehouse = Warehouse.objects.create(name="Principal")
        cls.other_warehouse = Warehouse.objects.create(name="Sucursal")
        cls.category = Category.objects.create(name="Ropa")

    @classmethod
    def tearDownClass(cls):
        TenantSettings.objects.filter(tenant=cls.tenant).delete()
        super().tearDownClass()

    _sku_counter = 0

    def _create_variant(
        self, *, name="Camiseta", barcode=None, price="20.00", stock="5"
    ):
        POSCatalogServiceTests._sku_counter += 1
        product = ProductVariantService.create_product(
            product_data={
                "type": "PRODUCT",
                "name": name,
                "category": self.category,
                "unit_of_measure": "UND",
            },
            variants_data=[
                {
                    "sku": f"SKU-POS-{POSCatalogServiceTests._sku_counter}",
                    "barcode": barcode,
                    "price": price,
                }
            ],
        )
        variant = product.variants.first()
        StockService.adjust_stock(
            variant=variant,
            warehouse=self.warehouse,
            counted_quantity=Decimal(stock),
            concept="ADJUSTMENT",
            user=self.user,
        )
        return variant

    def test_search_exact_barcode_match_wins_over_fuzzy_results(self):
        scanned = self._create_variant(name="Camiseta Roja", barcode="7501234567890")
        # Otro producto cuyo nombre tambien matchearia una busqueda difusa
        # por "7501234567890" si el barcode no tuviera prioridad -no debiera
        # aparecer en el resultado.
        self._create_variant(name="Producto 7501234567890 promocionado")

        results = POSCatalogService.search(
            warehouse=self.warehouse, query="7501234567890"
        )

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["id"], scanned.id)

    def test_search_falls_back_to_fuzzy_name_and_sku_match(self):
        variant = self._create_variant(name="Pantalon Azul")

        by_name = POSCatalogService.search(warehouse=self.warehouse, query="Pantalon")
        self.assertEqual([row["id"] for row in by_name], [variant.id])

        by_sku = POSCatalogService.search(warehouse=self.warehouse, query=variant.sku)
        self.assertEqual([row["id"] for row in by_sku], [variant.id])

    def test_search_no_match_returns_empty_list(self):
        self._create_variant(name="Camiseta")
        results = POSCatalogService.search(
            warehouse=self.warehouse, query="inexistente"
        )
        self.assertEqual(results, [])

    def test_catalog_includes_stock_for_the_given_warehouse_only(self):
        variant = self._create_variant(stock="7")

        catalog_here = POSCatalogService.catalog(warehouse=self.warehouse)
        row = next(row for row in catalog_here if row["id"] == variant.id)
        self.assertEqual(row["stock"], "7.000")

        catalog_elsewhere = POSCatalogService.catalog(warehouse=self.other_warehouse)
        row_elsewhere = next(
            row for row in catalog_elsewhere if row["id"] == variant.id
        )
        self.assertEqual(row_elsewhere["stock"], "0")

    def test_catalog_includes_active_promotion_for_variant(self):
        variant = self._create_variant(price="100.00")
        promotion = Promotion.objects.create(
            name="20% descuento",
            type="PERCENTAGE",
            value="20.00",
            start_date=timezone.now() - timedelta(days=1),
            end_date=timezone.now() + timedelta(days=1),
            is_active=True,
        )
        PromotionProduct.objects.create(promotion=promotion, variant=variant)

        catalog = POSCatalogService.catalog(warehouse=self.warehouse)
        row = next(row for row in catalog if row["id"] == variant.id)
        self.assertIsNotNone(row["promotion"])
        self.assertEqual(row["promotion"]["type"], "PERCENTAGE")
        self.assertEqual(row["promotion"]["value"], "20.0000")

    def test_catalog_variant_without_promotion_has_none(self):
        variant = self._create_variant()
        catalog = POSCatalogService.catalog(warehouse=self.warehouse)
        row = next(row for row in catalog if row["id"] == variant.id)
        self.assertIsNone(row["promotion"])

    def test_catalog_excludes_inactive_and_non_sellable_products(self):
        variant = self._create_variant(name="Descontinuado")
        variant.is_active = False
        variant.save(update_fields=["is_active"])

        catalog = POSCatalogService.catalog(warehouse=self.warehouse)
        self.assertNotIn(variant.id, [row["id"] for row in catalog])

    def test_catalog_includes_unit_of_measure_for_scale_integration(self):
        # Sprint 27: el POS usa esto para saber si debe activar la lectura
        # de balanza al agregar la variante al carrito.
        variant = self._create_variant()
        catalog = POSCatalogService.catalog(warehouse=self.warehouse)
        row = next(row for row in catalog if row["id"] == variant.id)
        self.assertEqual(row["unit_of_measure"], "UND")
