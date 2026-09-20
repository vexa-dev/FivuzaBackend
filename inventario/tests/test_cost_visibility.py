# Bloque A.5: el costo y el margen del negocio no viajan a quien no tiene
# INVENTORY_VIEW_COST -se quitan del payload, no se esconden en la UI.
from decimal import Decimal

from django.core.cache import cache
from django_tenants.test.cases import TenantTestCase
from rest_framework.test import APIClient

from core.models import TenantSettings
from inventario.models import Category, Warehouse
from inventario.services import ProductVariantService, StockService
from usuarios.models import Permission, Role, RolePermission, User, UserWarehouse


class CostVisibilityTests(TenantTestCase):
    @classmethod
    def get_test_schema_name(cls):
        return "test_inventario_cost_visibility"

    @classmethod
    def get_test_tenant_domain(cls):
        return "test-inventario-cost-visibility.test.com"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.password = "ClaveSegura123"
        cls.admin_user = cls._create_user(
            "admin@negocio.com", Role.objects.get(name="admin")
        )
        cls.seller_user = cls._create_user(
            "vendedor@negocio.com", Role.objects.get(name="seller")
        )

        # Administra el catalogo pero no ve costos: el caso real del
        # "cataloguero" que carga productos sin acceso a los margenes.
        cls.catalog_role = Role.objects.create(name="catalogador")
        for code in ("INVENTORY_VIEW", "INVENTORY_MANAGE"):
            RolePermission.objects.create(
                role=cls.catalog_role, permission=Permission.objects.get(code=code)
            )
        cls.catalog_user = cls._create_user("catalogo@negocio.com", cls.catalog_role)

        cls.warehouse = Warehouse.objects.create(name="Principal")
        UserWarehouse.objects.create(user=cls.seller_user, warehouse=cls.warehouse)
        UserWarehouse.objects.create(user=cls.catalog_user, warehouse=cls.warehouse)
        cls.category = Category.objects.create(name="Ropa")

        product = ProductVariantService.create_product(
            product_data={
                "type": "PRODUCT",
                "name": "Camiseta",
                "category": cls.category,
                "unit_of_measure": "UND",
            },
            variants_data=[
                {"sku": "SKU-COST-1", "cost": "10.00", "price": "25.00"},
            ],
        )
        cls.product = product
        cls.variant = product.variants.first()
        StockService.adjust_stock(
            variant=cls.variant,
            warehouse=cls.warehouse,
            counted_quantity=Decimal("5"),
            concept="ADJUSTMENT",
            user=cls.admin_user,
        )

    @classmethod
    def _create_user(cls, email, role):
        user = User.objects.create(email=email, role=role)
        user.set_password(cls.password)
        user.save()
        return user

    @classmethod
    def tearDownClass(cls):
        TenantSettings.objects.filter(tenant=cls.tenant).delete()
        super().tearDownClass()

    def setUp(self):
        cache.clear()

    def _client_as(self, user):
        client = APIClient(HTTP_HOST=self.domain.domain)
        login = client.post(
            "/api/v1/auth/login/",
            {"email": user.email, "password": self.password},
            format="json",
        )
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {login.data['access']}")
        return client

    def test_variant_endpoint_hides_cost_from_the_cashier(self):
        response = self._client_as(self.seller_user).get(
            f"/api/v1/inventario/product-variants/{self.variant.id}/"
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("cost", response.data)

    def test_variant_endpoint_keeps_cost_for_whoever_may_see_it(self):
        response = self._client_as(self.admin_user).get(
            f"/api/v1/inventario/product-variants/{self.variant.id}/"
        )
        self.assertEqual(response.data["cost"], "10.0000")

    def test_nested_variants_of_a_product_also_hide_cost(self):
        response = self._client_as(self.seller_user).get(
            f"/api/v1/inventario/products/{self.product.id}/"
        )
        self.assertEqual(response.status_code, 200)
        for variant in response.data["variants"]:
            self.assertNotIn("cost", variant)

    def test_stock_valuation_report_is_denied_without_the_permission(self):
        response = self._client_as(self.seller_user).get(
            "/api/v1/inventario/reports/stock-valuation/"
        )
        self.assertEqual(response.status_code, 403)

        allowed = self._client_as(self.admin_user).get(
            "/api/v1/inventario/reports/stock-valuation/"
        )
        self.assertEqual(allowed.status_code, 200)

    def test_dashboard_hides_gross_margin_from_the_cashier(self):
        response = self._client_as(self.seller_user).get("/api/v1/dashboard/metrics/")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("gross_margin", response.data)

        supervisor = self._client_as(self.admin_user).get("/api/v1/dashboard/metrics/")
        self.assertIn("gross_margin", supervisor.data)

    def test_catalog_manager_cannot_write_a_cost_he_cannot_see(self):
        """Bloque A.5: ver y escribir el costo van juntos. Quien administra
        el catalogo sin INVENTORY_VIEW_COST edita todo lo demas, pero no
        puede pisar un valor que nunca vio."""
        client = self._client_as(self.catalog_user)
        response = client.patch(
            f"/api/v1/inventario/product-variants/{self.variant.id}/",
            {"cost": "1.00"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.variant.refresh_from_db()
        self.assertEqual(str(self.variant.cost), "10.0000")

        # El resto de la variante si lo edita con normalidad.
        allowed = client.patch(
            f"/api/v1/inventario/product-variants/{self.variant.id}/",
            {"price": "30.00"},
            format="json",
        )
        self.assertEqual(allowed.status_code, 200)

    def test_cost_cannot_be_set_when_creating_a_product_either(self):
        response = self._client_as(self.catalog_user).post(
            "/api/v1/inventario/products/",
            {
                "type": "PRODUCT",
                "name": "Producto sin permiso de costo",
                "category": self.category.id,
                "unit_of_measure": "UND",
                "variants_input": [{"sku": "SKU-COST-DENIED", "cost": "99.00"}],
            },
            format="json",
        )
        self.assertEqual(response.status_code, 400)

        # Sin el campo costo, la creacion pasa.
        allowed = self._client_as(self.catalog_user).post(
            "/api/v1/inventario/products/",
            {
                "type": "PRODUCT",
                "name": "Producto sin costo declarado",
                "category": self.category.id,
                "unit_of_measure": "UND",
                "variants_input": [{"sku": "SKU-COST-OK", "price": "10.00"}],
            },
            format="json",
        )
        self.assertEqual(allowed.status_code, 201)

    def test_whoever_sees_the_cost_can_still_write_it(self):
        response = self._client_as(self.admin_user).patch(
            f"/api/v1/inventario/product-variants/{self.variant.id}/",
            {"cost": "12.00"},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.variant.refresh_from_db()
        self.assertEqual(str(self.variant.cost), "12.0000")
