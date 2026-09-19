# Pruebas de SchemaRouteGuardMiddleware y run_per_tenant (endurecimiento
# para produccion).
import json
from types import SimpleNamespace
from unittest import mock

from django.http import HttpResponse
from django.test import RequestFactory, SimpleTestCase
from django_tenants.test.cases import TenantTestCase

from core.middleware import SchemaRouteGuardMiddleware
from core.models import TenantSettings
from core.tenant_tasks import run_per_tenant


class SchemaRouteGuardTests(SimpleTestCase):
    """Unitarios del middleware: se simula request.tenant en vez de crear
    tenants reales (crear/borrar el tenant public en la suite contamina a
    las demas clases de prueba)."""

    def setUp(self):
        self.factory = RequestFactory()
        self.middleware = SchemaRouteGuardMiddleware(
            lambda request: HttpResponse("vista")
        )

    def _get(self, path, schema_name):
        request = self.factory.get(path)
        request.tenant = SimpleNamespace(schema_name=schema_name)
        return self.middleware(request)

    def test_business_route_on_public_domain_is_404_not_500(self):
        response = self._get("/api/v1/inventario/categories/", "public")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(json.loads(response.content)["error"]["code"], "NOT_FOUND")

    def test_platform_and_admin_routes_on_tenant_domain_are_404(self):
        for path in (
            "/api/v1/platform/auth/login/",
            "/api/v1/core/tenants/",
            "/admin/",
        ):
            self.assertEqual(self._get(path, "negocio").status_code, 404, path)

    def test_shared_routes_answer_on_both_domains(self):
        for schema in ("public", "negocio"):
            for path in ("/api/v1/health/", "/api/v1/core/legal/terms/"):
                self.assertEqual(
                    self._get(path, schema).status_code, 200, (schema, path)
                )

    def test_matching_routes_reach_the_view(self):
        self.assertEqual(
            self._get("/api/v1/inventario/categories/", "negocio").status_code, 200
        )
        self.assertEqual(
            self._get("/api/v1/platform/auth/login/", "public").status_code, 200
        )


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
