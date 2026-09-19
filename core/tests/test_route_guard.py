# Pruebas de SchemaRouteGuardMiddleware y run_per_tenant (endurecimiento
# para produccion).
from unittest import mock

from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import get_public_schema_name, schema_context
from rest_framework.test import APIClient

from core.models import Domain, Tenant, TenantSettings
from core.tenant_tasks import run_per_tenant


class SchemaRouteGuardTests(TenantTestCase):
    @classmethod
    def get_test_schema_name(cls):
        return "test_route_guard"

    @classmethod
    def get_test_tenant_domain(cls):
        return "test-route-guard.test.com"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # django-tenants solo permite crear tenants desde el esquema public;
        # TenantTestCase deja la conexion en el esquema del tenant de prueba.
        with schema_context(get_public_schema_name()):
            public_tenant, cls._created_public = Tenant.objects.get_or_create(
                schema_name="public", defaults={"company_name": "Servicio Publico"}
            )
            cls.public_domain, _ = Domain.objects.get_or_create(
                domain="public.localhost",
                defaults={"tenant": public_tenant, "is_primary": True},
            )

    @classmethod
    def tearDownClass(cls):
        TenantSettings.objects.filter(tenant=cls.tenant).delete()
        with schema_context(get_public_schema_name()):
            cls.public_domain.delete()
            if cls._created_public:
                Tenant.objects.filter(schema_name="public").delete()
        super().tearDownClass()

    def test_business_route_on_public_domain_is_404_not_500(self):
        response = APIClient(HTTP_HOST="public.localhost").get(
            "/api/v1/inventario/categories/"
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"]["code"], "NOT_FOUND")

    def test_platform_route_on_tenant_domain_is_404(self):
        response = APIClient(HTTP_HOST=self.domain.domain).post(
            "/api/v1/platform/auth/login/",
            {"email": "x@fivuza.com", "password": "x"},
            format="json",
        )
        self.assertEqual(response.status_code, 404)

    def test_shared_routes_answer_on_both_domains(self):
        for host in ("public.localhost", self.domain.domain):
            response = APIClient(HTTP_HOST=host).get("/api/v1/core/legal/terms/")
            self.assertNotEqual(response.status_code, 404, host)

    def test_business_route_on_tenant_domain_reaches_the_view(self):
        response = APIClient(HTTP_HOST=self.domain.domain).get(
            "/api/v1/inventario/categories/"
        )
        # Sin token: la vista responde 401, no el 404 del middleware.
        self.assertEqual(response.status_code, 401)


class RunPerTenantTests(TenantTestCase):
    @classmethod
    def get_test_schema_name(cls):
        return "test_run_per_tenant"

    @classmethod
    def get_test_tenant_domain(cls):
        return "test-run-per-tenant.test.com"

    @classmethod
    def tearDownClass(cls):
        TenantSettings.objects.filter(tenant=cls.tenant).delete()
        super().tearDownClass()

    def test_a_failing_tenant_does_not_stop_the_others(self):
        seen = []

        def fn(tenant):
            seen.append(tenant.schema_name)
            if tenant.schema_name == self.tenant.schema_name:
                raise RuntimeError("tenant roto")

        with mock.patch("core.tenant_tasks.logger") as logger:
            failed = run_per_tenant("tarea_de_prueba", fn)

        self.assertEqual(failed, [self.tenant.schema_name])
        self.assertIn(self.tenant.schema_name, seen)
        logger.exception.assert_called_once()
