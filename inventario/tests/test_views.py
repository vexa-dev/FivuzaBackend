# Pruebas de ViewSets/vistas: permisos, serialización, códigos de respuesta HTTP.
from django.core.cache import cache
from django_tenants.test.cases import TenantTestCase
from rest_framework.test import APIClient

from core.models import TenantSettings
from inventario.models import Category, Supplier, Warehouse
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


class TenantCanceledPermissionTests(TenantTestCase):
    """TenantNotCanceled (Sprint 8, Especificacion de API §4.12): a diferencia
    de un tenant suspended (bloquea todo), un tenant canceled conserva
    lectura durante su periodo de gracia -solo bloquea escritura."""

    @classmethod
    def get_test_schema_name(cls):
        return "test_tenant_canceled"

    @classmethod
    def get_test_tenant_domain(cls):
        return "test-tenant-canceled.test.com"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.password = "ClaveSegura123"
        cls.admin_user = User.objects.create(
            email="admin@negocio.com", role=Role.objects.get(name="admin")
        )
        cls.admin_user.set_password(cls.password)
        cls.admin_user.save()

    @classmethod
    def tearDownClass(cls):
        TenantSettings.objects.filter(tenant=cls.tenant).delete()
        super().tearDownClass()

    def setUp(self):
        cache.clear()
        self.tenant.status = "canceled"
        self.tenant.save(update_fields=["status"])

    def tearDown(self):
        self.tenant.status = "active"
        self.tenant.save(update_fields=["status"])

    def _client(self):
        client = APIClient(HTTP_HOST=self.domain.domain)
        login = client.post(
            "/api/v1/auth/login/",
            {"email": self.admin_user.email, "password": self.password},
            format="json",
        )
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {login.data['access']}")
        return client

    def test_canceled_tenant_can_still_read(self):
        response = self._client().get("/api/v1/inventario/categories/")
        self.assertEqual(response.status_code, 200)

    def test_canceled_tenant_cannot_write(self):
        response = self._client().post(
            "/api/v1/inventario/categories/", {"name": "Nueva"}, format="json"
        )
        self.assertEqual(response.status_code, 402)
        self.assertEqual(response.data["error"]["code"], "TENANT_CANCELED")


class PurchaseOrderEndpointsTests(TenantTestCase):
    """CRUD + accion receive de purchase-orders: gateado por
    RequiresFeature('HAS_PURCHASES_MODULE') y por PURCHASES_MANAGE (Sprint 5)."""

    @classmethod
    def get_test_schema_name(cls):
        return "test_inventario_purchases_views"

    @classmethod
    def get_test_tenant_domain(cls):
        return "test-inventario-purchases-views.test.com"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
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

        cls.warehouse = Warehouse.objects.create(name="Principal")
        cls.supplier = Supplier.objects.create(
            ruc_or_dni="20123456789", company_name="Proveedor SAC"
        )
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

    def _create_variant(self, sku="PO-SKU"):
        from inventario.services import ProductVariantService

        product = ProductVariantService.create_product(
            product_data={
                "type": "PRODUCT",
                "name": "Producto de compra",
                "category": self.category,
                "unit_of_measure": "UND",
            },
            variants_data=[{"sku": sku, "cost": "5.00"}],
        )
        return product.variants.first()

    def test_purchases_blocked_by_default_feature_flag(self):
        # TenantSettings.purchases_enabled es True por defecto en el modelo,
        # pero el signal de aprovisionamiento no lo toca -se fuerza aqui
        # para probar el otro lado del flag.
        settings = TenantSettings.objects.get(tenant=self.tenant)
        settings.purchases_enabled = False
        settings.save(update_fields=["purchases_enabled"])

        response = self._client_as(self.admin_user).get(
            "/api/v1/inventario/purchase-orders/"
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data["code"], "MODULE_DISABLED")

        settings.purchases_enabled = True
        settings.save(update_fields=["purchases_enabled"])

    def test_seller_cannot_create_purchase_order(self):
        variant = self._create_variant()
        response = self._client_as(self.seller_user).post(
            "/api/v1/inventario/purchase-orders/",
            {
                "supplier": self.supplier.id,
                "warehouse": self.warehouse.id,
                "details_input": [
                    {"variant_id": variant.id, "quantity": "5", "unit_cost": "10.00"}
                ],
            },
            format="json",
        )
        self.assertEqual(response.status_code, 403)

    def test_admin_creates_and_receives_purchase_order(self):
        variant = self._create_variant()
        admin = self._client_as(self.admin_user)

        create_response = admin.post(
            "/api/v1/inventario/purchase-orders/",
            {
                "supplier": self.supplier.id,
                "warehouse": self.warehouse.id,
                "details_input": [
                    {"variant_id": variant.id, "quantity": "5", "unit_cost": "10.00"}
                ],
            },
            format="json",
        )
        self.assertEqual(create_response.status_code, 201)
        self.assertEqual(create_response.data["status"], "PENDING")
        self.assertEqual(create_response.data["total"], "50.0000")

        order_id = create_response.data["id"]
        receive_response = admin.post(
            f"/api/v1/inventario/purchase-orders/{order_id}/receive/"
        )
        self.assertEqual(receive_response.status_code, 200)
        self.assertEqual(receive_response.data["status"], "RECEIVED")
        self.assertEqual(receive_response.data["movements_created"], 1)

        stock_response = admin.get(
            f"/api/v1/inventario/stock/?variant={variant.id}&warehouse={self.warehouse.id}"
        )
        self.assertEqual(stock_response.data[0]["quantity"], "5.000")


class CatalogImportEndpointsTests(TenantTestCase):
    """POST /inventario/catalog-import/ y GET .../template/ (Sprint 6)."""

    @classmethod
    def get_test_schema_name(cls):
        return "test_inventario_catalog_import_views"

    @classmethod
    def get_test_tenant_domain(cls):
        return "test-inventario-catalog-import-views.test.com"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
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

        Category.objects.create(name="Ropa")

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

    def test_template_download(self):
        response = self._client_as(self.admin_user).get(
            "/api/v1/inventario/catalog-import/template/"
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("nombre_producto", response.content.decode())

    def test_seller_cannot_import(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        csv_content = b"nombre_producto,categoria,sku,codigo_barras,costo,precio,stock_minimo,cantidad_inicial,almacen\n"
        upload = SimpleUploadedFile(
            "catalogo.csv", csv_content, content_type="text/csv"
        )
        response = self._client_as(self.seller_user).post(
            "/api/v1/inventario/catalog-import/", {"file": upload}, format="multipart"
        )
        self.assertEqual(response.status_code, 403)

    def test_admin_imports_valid_csv(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        csv_content = (
            "nombre_producto,categoria,sku,codigo_barras,costo,precio,"
            "stock_minimo,cantidad_inicial,almacen\n"
            "Camiseta,Ropa,SKU-CSV-1,,10,20,0,0,\n"
        ).encode()
        upload = SimpleUploadedFile(
            "catalogo.csv", csv_content, content_type="text/csv"
        )
        response = self._client_as(self.admin_user).post(
            "/api/v1/inventario/catalog-import/", {"file": upload}, format="multipart"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["created"], 1)
        self.assertEqual(response.data["errors"], 0)
