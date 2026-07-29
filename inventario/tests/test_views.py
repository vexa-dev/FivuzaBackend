# Pruebas de ViewSets/vistas: permisos, serialización, códigos de respuesta HTTP.
from django.core.cache import cache
from django_tenants.test.cases import TenantTestCase
from rest_framework.test import APIClient

from core.models import TenantSettings
from inventario.models import Category
from usuarios.models import Role, User


class InventoryCatalogEndpointsTests(TenantTestCase):
    """CRUD de catalogo y aplicacion de HasInventoryAccess (Sprint 3)."""

    @classmethod
    def get_test_schema_name(cls):
        return "test_inventario_views"

    @classmethod
    def get_test_tenant_domain(cls):
        return "test-inventario-views.test.com"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # TenantProvisioningService.seed_default_roles() (post_schema_sync)
        # ya sembro admin/manager/seller con INVENTORY_VIEW/INVENTORY_MANAGE.
        cls.password = "ClaveSegura123"
        cls.admin_role = Role.objects.get(name="admin")
        cls.seller_role = Role.objects.get(name="seller")

        cls.admin_user = User.objects.create(
            email="admin@negocio.com", role=cls.admin_role
        )
        cls.admin_user.set_password(cls.password)
        cls.admin_user.save()

        cls.seller_user = User.objects.create(
            email="vendedor@negocio.com", role=cls.seller_role
        )
        cls.seller_user.set_password(cls.password)
        cls.seller_user.save()

        cls.category = Category.objects.create(name="Ropa")

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

    def test_seller_can_read_catalog_but_not_write(self):
        seller = self._client_as(self.seller_user)
        self.assertEqual(seller.get("/api/v1/inventario/categories/").status_code, 200)
        response = seller.post(
            "/api/v1/inventario/categories/", {"name": "Nueva"}, format="json"
        )
        self.assertEqual(response.status_code, 403)

    def test_admin_can_manage_catalog(self):
        response = self._client_as(self.admin_user).post(
            "/api/v1/inventario/categories/", {"name": "Calzado"}, format="json"
        )
        self.assertEqual(response.status_code, 201)

    def test_deleting_category_soft_deletes_and_excludes_from_default_manager(self):
        target = Category.objects.create(name="Temporal")
        response = self._client_as(self.admin_user).delete(
            f"/api/v1/inventario/categories/{target.id}/"
        )
        self.assertEqual(response.status_code, 204)
        self.assertFalse(Category.objects.filter(id=target.id).exists())
        self.assertTrue(Category.all_objects.filter(id=target.id).exists())

    def test_create_product_without_variants_generates_default_variant(self):
        response = self._client_as(self.admin_user).post(
            "/api/v1/inventario/products/",
            {
                "type": "PRODUCT",
                "name": "Polo basico",
                "category": self.category.id,
                "unit_of_measure": "UND",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(len(response.data["variants"]), 1)
        self.assertTrue(response.data["variants"][0]["is_default"])

    def test_create_product_with_variant_matrix(self):
        settings = TenantSettings.objects.get(tenant=self.tenant)
        settings.variants_enabled = True
        settings.save(update_fields=["variants_enabled"])

        response = self._client_as(self.admin_user).post(
            "/api/v1/inventario/products/",
            {
                "type": "PRODUCT",
                "name": "Polo con tallas",
                "category": self.category.id,
                "unit_of_measure": "UND",
                "variants_input": [
                    {"sku": "POLO-S", "price": "39.90"},
                    {"sku": "POLO-M", "price": "39.90"},
                ],
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(len(response.data["variants"]), 2)

    def test_cannot_delete_the_only_variant_of_a_product(self):
        admin = self._client_as(self.admin_user)
        product_response = admin.post(
            "/api/v1/inventario/products/",
            {
                "type": "PRODUCT",
                "name": "Producto unico",
                "category": self.category.id,
                "unit_of_measure": "UND",
            },
            format="json",
        )
        variant_id = product_response.data["variants"][0]["id"]

        response = admin.delete(f"/api/v1/inventario/product-variants/{variant_id}/")
        self.assertEqual(response.status_code, 400)

    def test_second_warehouse_blocked_without_multi_warehouse_flag(self):
        admin = self._client_as(self.admin_user)
        first = admin.post(
            "/api/v1/inventario/warehouses/",
            {"name": "Principal"},
            format="json",
        )
        self.assertEqual(first.status_code, 201)

        second = admin.post(
            "/api/v1/inventario/warehouses/",
            {"name": "Sucursal 2"},
            format="json",
        )
        self.assertEqual(second.status_code, 403)
        self.assertEqual(second.data["code"], "MODULE_DISABLED")

    def test_multi_warehouse_flag_allows_second_warehouse(self):
        settings = TenantSettings.objects.get(tenant=self.tenant)
        settings.multi_warehouse_enabled = True
        settings.save(update_fields=["multi_warehouse_enabled"])

        admin = self._client_as(self.admin_user)
        admin.post(
            "/api/v1/inventario/warehouses/", {"name": "Principal"}, format="json"
        )
        second = admin.post(
            "/api/v1/inventario/warehouses/", {"name": "Sucursal 2"}, format="json"
        )
        self.assertEqual(second.status_code, 201)

    def test_barcode_filter_finds_variant(self):
        admin = self._client_as(self.admin_user)
        admin.post(
            "/api/v1/inventario/products/",
            {
                "type": "PRODUCT",
                "name": "Producto con codigo de barras",
                "category": self.category.id,
                "unit_of_measure": "UND",
                "variants_input": [{"sku": "BAR-1", "barcode": "7501234567890"}],
            },
            format="json",
        )
        response = admin.get(
            "/api/v1/inventario/product-variants/?barcode=7501234567890"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["barcode"], "7501234567890")
