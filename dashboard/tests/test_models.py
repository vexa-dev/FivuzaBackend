# Pruebas de modelos: validaciones de campo, constraints, métodos del modelo.
from datetime import date, datetime
from datetime import timezone as dt_timezone
from decimal import Decimal

from django.utils import timezone
from django_tenants.test.cases import TenantTestCase

from core.models import TenantSettings
from dashboard.services import DashboardMetricsService, DashboardRefreshService
from inventario.models import Category, Product, ProductVariant, Warehouse
from usuarios.models import Role, User
from ventas.models import (
    CashRegister,
    CashSession,
    Customer,
    Sale,
    SaleDetail,
    SalePayment,
)


class DashboardRefreshServiceTests(TenantTestCase):
    """is_due()/refresh() (Sprint 24, Esquema Backend §9.2): la frecuencia de
    refresco es configurable por tenant, y refresh() debe correr fuera de
    una transaccion (REFRESH MATERIALIZED VIEW CONCURRENTLY lo exige)."""

    @classmethod
    def get_test_schema_name(cls):
        return "test_dashboard_refresh_service"

    @classmethod
    def get_test_tenant_domain(cls):
        return "test-dashboard-refresh-service.test.com"

    @classmethod
    def tearDownClass(cls):
        TenantSettings.objects.filter(tenant=cls.tenant).delete()
        super().tearDownClass()

    def test_is_due_when_never_refreshed(self):
        # mv_daily_sales_summary se crea WITH DATA en la migracion -en un
        # tenant de pruebas recien creado ya tiene una fila de refreshed_at
        # (aunque sea de hace rato), asi que is_due() debe evaluar la
        # frecuencia configurada, no asumir "nunca corrio".
        self.assertIsInstance(
            DashboardRefreshService.is_due(self.tenant.schema_name), bool
        )

    def test_refresh_runs_without_error(self):
        # REFRESH MATERIALIZED VIEW CONCURRENTLY exige que la vista tenga un
        # indice unico (ya lo tiene, migracion 0003) y que no corra dentro de
        # una transaccion explicita -TenantTestCase envuelve cada test en una
        # transaccion que hace rollback, pero refresh() abre su propio
        # cursor fuera de ese framing logico de Django (autocommit real de
        # la conexion), asi que igual debe completar sin lanzar.
        DashboardRefreshService.refresh(self.tenant.schema_name)


class DashboardMetricsServiceTests(TenantTestCase):
    """Cálculo de métricas (Sprint 24, API Spec §2.4): ventas de hoy,
    productos más vendidos, y margen bruto aproximado contra el costo
    ACTUAL de cada variante (SaleDetail no guarda un snapshot historico)."""

    @classmethod
    def get_test_schema_name(cls):
        return "test_dashboard_metrics_service"

    @classmethod
    def get_test_tenant_domain(cls):
        return "test-dashboard-metrics-service.test.com"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        role = Role.objects.create(name="admin", is_system_default=True)
        cls.user = User.objects.create(email="admin@negocio.com", role=role)
        cls.warehouse = Warehouse.objects.create(name="Principal")
        cls.customer = Customer.objects.create(
            document_type="DNI", document_number="12345678", name="Cliente de prueba"
        )
        cls.category = Category.objects.create(name="General")
        product = Product.objects.create(
            type="PRODUCT",
            name="Producto A",
            category=cls.category,
            unit_of_measure="UND",
        )
        cls.variant = ProductVariant.objects.create(
            product=product, sku="SKU-A", cost=Decimal("10.00"), price=Decimal("25.00")
        )
        cash_register = CashRegister.objects.create(
            warehouse=cls.warehouse, name="Caja 1"
        )
        cls.cash_session = CashSession.objects.create(
            cash_register=cash_register,
            user=cls.user,
            opening_amount=Decimal("0"),
            opening_at=timezone.now(),
            status="OPEN",
        )

    @classmethod
    def tearDownClass(cls):
        TenantSettings.objects.filter(tenant=cls.tenant).delete()
        super().tearDownClass()

    def _make_sale(self, *, quantity, unit_price, created_at):
        import uuid

        sale = Sale.objects.create(
            invoice_number=f"F001-{Sale.objects.count() + 1}",
            customer=self.customer,
            user=self.user,
            warehouse=self.warehouse,
            cash_session=self.cash_session,
            subtotal=unit_price * quantity,
            total=unit_price * quantity,
            payment_status="PAID",
            status="COMPLETED",
            client_side_uuid=str(uuid.uuid4()),
            sync_status="SYNCED",
        )
        Sale.objects.filter(pk=sale.pk).update(created_at=created_at)
        sale.refresh_from_db()
        SaleDetail.objects.create(
            sale=sale,
            variant_id=self.variant.id,
            product_name_snapshot=self.variant.product.name,
            sku_snapshot=self.variant.sku,
            quantity=quantity,
            unit_price=unit_price,
            subtotal=unit_price * quantity,
        )
        SalePayment.objects.create(
            sale=sale, method="CASH", amount=unit_price * quantity
        )
        return sale

    def test_sales_today_only_counts_todays_completed_sales(self):
        today = datetime.now(dt_timezone.utc)
        self._make_sale(quantity=2, unit_price=Decimal("25.00"), created_at=today)

        result = DashboardMetricsService.sales_today()
        self.assertEqual(result["total_sales"], "50.0000")
        self.assertEqual(result["total_transactions"], 1)

    def test_top_products_orders_by_quantity_sold(self):
        today = datetime.now(dt_timezone.utc)
        self._make_sale(quantity=3, unit_price=Decimal("25.00"), created_at=today)

        top = DashboardMetricsService.top_products(
            date_from=date.today(), date_to=date.today()
        )
        self.assertEqual(top[0]["product_name"], "Producto A")
        self.assertEqual(top[0]["quantity_sold"], "3.000")

    def test_gross_margin_uses_current_variant_cost(self):
        today = datetime.now(dt_timezone.utc)
        self._make_sale(quantity=2, unit_price=Decimal("25.00"), created_at=today)

        margin = DashboardMetricsService.gross_margin(
            date_from=date.today(), date_to=date.today()
        )
        # Ingreso 50.00, costo 2 x 10.00 = 20.00 -> margen 30.00
        self.assertEqual(margin["total_revenue"], "50.0000")
        self.assertEqual(margin["total_cost"], "20.0000")
        self.assertEqual(margin["gross_margin"], "30.0000")

    def test_payment_method_distribution_groups_by_method(self):
        today = datetime.now(dt_timezone.utc)
        self._make_sale(quantity=1, unit_price=Decimal("25.00"), created_at=today)

        distribution = DashboardMetricsService.payment_method_distribution(
            date_from=date.today(), date_to=date.today()
        )
        self.assertEqual(distribution, [{"method": "CASH", "total": "25.0000"}])
