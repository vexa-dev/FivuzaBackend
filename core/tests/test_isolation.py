# Aislamiento multi-tenant (TRD §7.2, prioridad #1 de testing, Sprint 6):
# un usuario de un tenant JAMAS puede leer/escribir datos de otro esquema,
# ni manipulando IDs a mano ni reusando un token entre subdominios.
from django.core.cache import cache
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import get_public_schema_name, schema_context
from rest_framework.test import APIClient

from core.models import Domain, Tenant, TenantSettings
from inventario.models import Category
from inventario.services import ProductVariantService
from usuarios.models import Role, User


class MultiTenantIsolationTests(TenantTestCase):
    """self.tenant (creado por TenantTestCase) hace de 'tenant A'; 'tenant B'
    se crea a mano en setUpClass -asi ambos son schemas reales, fisicamente
    separados por Postgres, no un mock de aislamiento."""

    @classmethod
    def get_test_schema_name(cls):
        return "test_isolation_a"

    @classmethod
    def get_test_tenant_domain(cls):
        return "test-isolation-a.test.com"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.password = "ClaveSegura123"

        # TenantProvisioningService.seed_default_roles() (post_schema_sync)
        # ya sembro admin/manager/seller con sus permisos al crear el
        # tenant -se reutiliza en vez de crear un rol duplicado sin permisos.
        role_a = Role.objects.get(name="admin")
        cls.user_a = User.objects.create(email="admin@negocio-a.com", role=role_a)
        cls.user_a.set_password(cls.password)
        cls.user_a.save()
        category_a = Category.objects.create(name="Categoria A")
        cls.product_a = ProductVariantService.create_product(
            product_data={
                "type": "PRODUCT",
                "name": "Producto A",
                "category": category_a,
                "unit_of_measure": "UND",
            },
            variants_data=[{"sku": "SKU-A"}],
        )

        # TenantTestCase deja la conexion apuntando al schema de tenant A
        # (connection.set_tenant en su setUpClass) -crear un tenant nuevo
        # requiere estar parado en el schema public.
        with schema_context(get_public_schema_name()):
            cls.tenant_b = Tenant.objects.create(
                schema_name="test_isolation_b", company_name="Negocio B"
            )
            cls.domain_b = Domain.objects.create(
                domain="test-isolation-b.test.com", tenant=cls.tenant_b, is_primary=True
            )
        with schema_context(cls.tenant_b.schema_name):
            role_b = Role.objects.get(name="admin")
            cls.user_b = User.objects.create(email="admin@negocio-b.com", role=role_b)
            cls.user_b.set_password(cls.password)
            cls.user_b.save()
            category_b = Category.objects.create(name="Categoria B")
            cls.product_b = ProductVariantService.create_product(
                product_data={
                    "type": "PRODUCT",
                    "name": "Producto B",
                    "category": category_b,
                    "unit_of_measure": "UND",
                },
                variants_data=[{"sku": "SKU-B"}],
            )

    @classmethod
    def tearDownClass(cls):
        TenantSettings.objects.filter(tenant=cls.tenant).delete()
        TenantSettings.objects.filter(tenant=cls.tenant_b).delete()
        cls.tenant_b.delete(force_drop=True)
        super().tearDownClass()

    def setUp(self):
        cache.clear()

    def _login(self, domain: str, email: str):
        client = APIClient(HTTP_HOST=domain)
        response = client.post(
            "/api/v1/auth/login/",
            {"email": email, "password": self.password},
            format="json",
        )
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {response.data['access']}")
        return client

    def test_ids_collide_across_tenants(self):
        # Cada schema tiene su propia secuencia de autoincremento -el primer
        # producto de cada tenant naturalmente comparte id=1. Es el escenario
        # exacto que un bug de aislamiento dejaria filtrar datos del tenant
        # equivocado.
        self.assertEqual(self.product_a.id, self.product_b.id)

    def test_user_a_reading_colliding_id_gets_only_tenant_a_data(self):
        client_a = self._login(self.domain.domain, self.user_a.email)
        response = client_a.get(f"/api/v1/inventario/products/{self.product_a.id}/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["name"], "Producto A")

    def test_user_a_product_list_never_includes_tenant_b_products(self):
        client_a = self._login(self.domain.domain, self.user_a.email)
        response = client_a.get("/api/v1/inventario/products/")
        names = [row["name"] for row in response.data]
        self.assertIn("Producto A", names)
        self.assertNotIn("Producto B", names)

    def test_user_b_product_list_never_includes_tenant_a_products(self):
        client_b = self._login(self.domain_b.domain, self.user_b.email)
        response = client_b.get("/api/v1/inventario/products/")
        names = [row["name"] for row in response.data]
        self.assertIn("Producto B", names)
        self.assertNotIn("Producto A", names)

    def test_token_issued_for_tenant_a_is_rejected_on_tenant_b_domain(self):
        login_client = APIClient(HTTP_HOST=self.domain.domain)
        login = login_client.post(
            "/api/v1/auth/login/",
            {"email": self.user_a.email, "password": self.password},
            format="json",
        )
        access_token = login.data["access"]

        client_on_b = APIClient(HTTP_HOST=self.domain_b.domain)
        client_on_b.credentials(HTTP_AUTHORIZATION=f"Bearer {access_token}")
        response = client_on_b.get("/api/v1/inventario/products/")
        self.assertEqual(response.status_code, 401)

    def test_user_a_cannot_authenticate_with_email_that_only_exists_in_tenant_b(self):
        client_a = APIClient(HTTP_HOST=self.domain.domain)
        response = client_a.post(
            "/api/v1/auth/login/",
            {"email": self.user_b.email, "password": self.password},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
