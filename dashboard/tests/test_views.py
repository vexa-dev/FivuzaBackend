# Pruebas de ViewSets/vistas: permisos, serialización, códigos de respuesta HTTP.
from channels.testing import WebsocketCommunicator
from django_tenants.test.cases import TenantTestCase
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from core.models import TenantSettings
from usuarios.models import Role, User


class DashboardMetricsEndpointsTests(TenantTestCase):
    """GET /dashboard/metrics/ y CRUD de widgets (Sprint 24). No hay
    RequiresFeature ni HasModulePermission -el dashboard es visible para
    cualquier usuario autenticado del tenant, igual que en el frontend
    (sin requirePermission en la ruta /dashboard)."""

    @classmethod
    def get_test_schema_name(cls):
        return "test_dashboard_metrics_views"

    @classmethod
    def get_test_tenant_domain(cls):
        return "test-dashboard-metrics-views.test.com"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.password = "ClaveSegura123"
        role = Role.objects.get(name="admin")
        cls.user = User.objects.create(email="admin@negocio.com", role=role)
        cls.user.set_password(cls.password)
        cls.user.save()

    @classmethod
    def tearDownClass(cls):
        TenantSettings.objects.filter(tenant=cls.tenant).delete()
        super().tearDownClass()

    def setUp(self):
        from django.core.cache import cache

        cache.clear()

    def _client(self):
        client = APIClient(HTTP_HOST=self.domain.domain)
        login = client.post(
            "/api/v1/auth/login/",
            {"email": self.user.email, "password": self.password},
            format="json",
        )
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {login.data['access']}")
        return client

    def test_metrics_endpoint_returns_all_sections(self):
        response = self._client().get("/api/v1/dashboard/metrics/")
        self.assertEqual(response.status_code, 200)
        for key in (
            "today",
            "week",
            "month",
            "comparison_vs_previous_month",
            "top_products",
            "gross_margin",
            "critical_stock_count",
            "payment_method_distribution",
        ):
            self.assertIn(key, response.data)

    def test_widget_crud_is_scoped_to_the_requesting_user(self):
        client = self._client()
        create = client.post(
            "/api/v1/dashboard/widgets/",
            {"widget_code": "SALES_TODAY", "position": 1, "is_visible": True},
            format="json",
        )
        self.assertEqual(create.status_code, 201)
        self.assertEqual(create.data["user"], self.user.id)

        listing = client.get("/api/v1/dashboard/widgets/")
        self.assertEqual(len(listing.data), 1)


class DashboardConsumerTests(TenantTestCase):
    """DashboardConsumer (Sprint 24, TRD §2.5): acepta conexiones con un JWT
    valido que trae el claim schema_name de un tenant real, y rechaza sin
    token."""

    @classmethod
    def get_test_schema_name(cls):
        return "test_dashboard_consumer"

    @classmethod
    def get_test_tenant_domain(cls):
        return "test-dashboard-consumer.test.com"

    @classmethod
    def tearDownClass(cls):
        TenantSettings.objects.filter(tenant=cls.tenant).delete()
        super().tearDownClass()

    def _make_token(self) -> str:
        token = AccessToken()
        token["schema_name"] = self.tenant.schema_name
        return str(token)

    async def test_connect_with_valid_token_accepts(self):
        from config.asgi import application

        token = await self._async_make_token()
        communicator = WebsocketCommunicator(
            application, f"/ws/dashboard/?token={token}"
        )
        connected, _ = await communicator.connect()
        self.assertTrue(connected)
        await communicator.disconnect()

    async def test_connect_without_token_rejects(self):
        from config.asgi import application

        communicator = WebsocketCommunicator(application, "/ws/dashboard/")
        connected, close_code = await communicator.connect()
        self.assertFalse(connected)

    async def _async_make_token(self) -> str:
        from asgiref.sync import sync_to_async

        return await sync_to_async(self._make_token)()
