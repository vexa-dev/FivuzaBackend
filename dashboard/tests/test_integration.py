# Pruebas de flujo completo a través de la capa de servicios (ej. crear una venta
# de punta a punta), no solo de una unidad aislada.
from decimal import Decimal

from channels.testing import WebsocketCommunicator
from django.core.cache import cache
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase
from rest_framework_simplejwt.tokens import AccessToken

from core.models import TenantSettings
from dashboard.services import DashboardMetricsService
from inventario.models import Category, Warehouse
from inventario.services import ProductVariantService, StockService
from usuarios.models import Role, User
from ventas.models import CashRegister, CashSession, Customer
from ventas.services import SaleService


class SaleCompletedDashboardIntegrationTests(TenantTestCase):
    """SaleService.create_sale() -> DashboardBroadcastService (Sprint 24,
    TRD §2.5): una venta real de punta a punta debe (1) invalidar el cache
    de metricas para que el siguiente fetch vea la venta, y (2) empujar el
    evento sale_completed a un cliente conectado por WebSocket al grupo del
    tenant. Los tests unitarios existentes (test_models.py, test_views.py)
    cubren cada mitad por separado -esto cubre el cableado real entre
    ambas, que es lo unico que un test aislado no puede probar."""

    @classmethod
    def get_test_schema_name(cls):
        return "test_dashboard_sale_integration"

    @classmethod
    def get_test_tenant_domain(cls):
        return "test-dashboard-sale-integration.test.com"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        role = Role.objects.get(name="admin")
        cls.user = User.objects.create(email="admin@negocio.com", role=role)
        cls.warehouse = Warehouse.objects.create(name="Principal")
        cls.category = Category.objects.create(name="Ropa")
        cls.customer = Customer.objects.create(
            document_type="DNI", document_number="99999999", name="Cliente Dashboard"
        )

    @classmethod
    def tearDownClass(cls):
        TenantSettings.objects.filter(tenant=cls.tenant).delete()
        super().tearDownClass()

    def setUp(self):
        cache.clear()

    def _create_variant(self, *, price="20.00", stock_quantity="10"):
        product = ProductVariantService.create_product(
            product_data={
                "type": "PRODUCT",
                "name": "Camiseta",
                "category": self.category,
                "unit_of_measure": "UND",
            },
            variants_data=[{"sku": "SKU-DASH-1", "price": price}],
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
        register = CashRegister.objects.create(warehouse=self.warehouse, name="Caja 1")
        return CashSession.objects.create(
            cash_register=register,
            user=self.user,
            opening_amount="0",
            opening_at=timezone.now(),
            status="OPEN",
        )

    def _make_token(self) -> str:
        token = AccessToken()
        token["schema_name"] = self.tenant.schema_name
        token["user_id"] = self.user.id
        return str(token)

    def test_completed_sale_invalidates_the_metrics_cache(self):
        variant = self._create_variant()
        session = self._open_session()

        # Precarga el cache con una respuesta "vieja" -si create_sale no
        # invalida, el fetch de abajo devolveria esta venta cero en vez de
        # reflejar la venta real recien creada.
        DashboardMetricsService.get_all_metrics(warehouse_id=None)
        before = DashboardMetricsService.sales_today()
        self.assertEqual(before["total_transactions"], 0)

        SaleService.create_sale(
            customer=self.customer,
            cash_session=session,
            user=self.user,
            lines=[{"variant_id": variant.id, "quantity": "2"}],
            payments=[{"method": "CASH", "amount": Decimal("40.00")}],
        )

        after = DashboardMetricsService.get_all_metrics(warehouse_id=None)
        self.assertEqual(after["today"]["total_transactions"], 1)
        self.assertEqual(after["today"]["total_sales"], "40.0000")

    def test_scoped_metrics_are_cached_per_warehouse_set(self):
        """PR #79: get_all_metrics() dejaba de cachear apenas warehouse_ids
        no era None -un usuario no-admin recalculaba las metricas completas
        en cada carga del dashboard, sin ningun cache."""
        from unittest import mock

        self._create_variant()

        with mock.patch.object(
            DashboardMetricsService,
            "_compute_all_metrics",
            wraps=DashboardMetricsService._compute_all_metrics,
        ) as compute:
            DashboardMetricsService.get_all_metrics(warehouse_ids=(self.warehouse.id,))
            DashboardMetricsService.get_all_metrics(warehouse_ids=(self.warehouse.id,))
            self.assertEqual(compute.call_count, 1)

    def test_scoped_metrics_cache_key_ignores_warehouse_id_order(self):
        other_warehouse = Warehouse.objects.create(name="Sucursal Dashboard")
        from unittest import mock

        with mock.patch.object(
            DashboardMetricsService,
            "_compute_all_metrics",
            wraps=DashboardMetricsService._compute_all_metrics,
        ) as compute:
            DashboardMetricsService.get_all_metrics(
                warehouse_ids=(self.warehouse.id, other_warehouse.id)
            )
            DashboardMetricsService.get_all_metrics(
                warehouse_ids=(other_warehouse.id, self.warehouse.id)
            )
            self.assertEqual(compute.call_count, 1)

    async def test_completed_sale_broadcasts_to_a_connected_client(self):
        from asgiref.sync import sync_to_async

        from config.asgi import application

        variant = await sync_to_async(self._create_variant)()
        session = await sync_to_async(self._open_session)()
        token = await sync_to_async(self._make_token)()

        communicator = WebsocketCommunicator(
            application, f"/ws/dashboard/?token={token}"
        )
        connected, _ = await communicator.connect()
        self.assertTrue(connected)

        await sync_to_async(SaleService.create_sale)(
            customer=self.customer,
            cash_session=session,
            user=self.user,
            lines=[{"variant_id": variant.id, "quantity": "1"}],
            payments=[{"method": "CASH", "amount": Decimal("20.00")}],
        )

        event = await communicator.receive_json_from()
        self.assertEqual(event["event"], "sale_completed")
        self.assertEqual(event["warehouse_id"], self.warehouse.id)
        self.assertEqual(event["total"], "20.0000")

        await communicator.disconnect()
