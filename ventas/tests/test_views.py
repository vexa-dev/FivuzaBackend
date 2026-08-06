# Pruebas de ViewSets/vistas: permisos, apertura/cierre de caja, arqueo.
import os
from unittest import mock

from django.core import mail
from django.core.cache import cache
from django.test import override_settings
from django_tenants.test.cases import TenantTestCase
from rest_framework.test import APIClient

from core.models import TenantSettings
from inventario.models import Category, Warehouse
from inventario.services import ProductVariantService, StockService
from usuarios.models import Role, User
from ventas.models import CashMovement, CashRegister, CashSession, Customer, Promotion


class CashSessionEndpointsTests(TenantTestCase):
    @classmethod
    def get_test_schema_name(cls):
        return "test_ventas_cash"

    @classmethod
    def get_test_tenant_domain(cls):
        return "test-ventas-cash.test.com"

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
        cls.cash_register = CashRegister.objects.create(
            warehouse=cls.warehouse, name="Caja 1"
        )

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

    def test_seller_can_read_but_not_open_session(self):
        seller = self._client_as(self.seller_user)
        response = seller.get("/api/v1/ventas/cash-sessions/")
        self.assertEqual(response.status_code, 200)

        response = seller.post(
            "/api/v1/ventas/cash-sessions/open/",
            {"cash_register_id": self.cash_register.id, "opening_amount": "50.00"},
            format="json",
        )
        self.assertEqual(response.status_code, 403)

    def test_admin_can_open_session(self):
        response = self._client_as(self.admin_user).post(
            "/api/v1/ventas/cash-sessions/open/",
            {"cash_register_id": self.cash_register.id, "opening_amount": "50.00"},
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["status"], "OPEN")
        self.assertEqual(response.data["opening_amount"], "50.0000")

    def test_cannot_open_two_sessions_on_same_register(self):
        client = self._client_as(self.admin_user)
        client.post(
            "/api/v1/ventas/cash-sessions/open/",
            {"cash_register_id": self.cash_register.id, "opening_amount": "50.00"},
            format="json",
        )
        response = client.post(
            "/api/v1/ventas/cash-sessions/open/",
            {"cash_register_id": self.cash_register.id, "opening_amount": "20.00"},
            format="json",
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["error"]["code"], "CASH_SESSION_ALREADY_OPEN")

    def test_close_session_calculates_expected_amount_and_difference(self):
        client = self._client_as(self.admin_user)
        opened = client.post(
            "/api/v1/ventas/cash-sessions/open/",
            {"cash_register_id": self.cash_register.id, "opening_amount": "50.00"},
            format="json",
        )
        session_id = opened.data["id"]

        client.post(
            "/api/v1/ventas/cash-movements/",
            {
                "cash_session": session_id,
                "type": "IN",
                "concept": "AJUSTE",
                "amount": "10.00",
            },
            format="json",
        )
        client.post(
            "/api/v1/ventas/cash-movements/",
            {
                "cash_session": session_id,
                "type": "OUT",
                "concept": "RETIRO",
                "amount": "5.00",
            },
            format="json",
        )

        response = client.post(
            f"/api/v1/ventas/cash-sessions/{session_id}/close/",
            {"counted_closing_amount": "54.00"},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], "CLOSED")
        # 50 + 10 - 5 = 55 esperado; contado 54 -> diferencia -1
        self.assertEqual(response.data["expected_closing_amount"], "55.0000")
        self.assertEqual(response.data["difference"], "-1.0000")

    def test_cannot_close_an_already_closed_session(self):
        client = self._client_as(self.admin_user)
        opened = client.post(
            "/api/v1/ventas/cash-sessions/open/",
            {"cash_register_id": self.cash_register.id, "opening_amount": "50.00"},
            format="json",
        )
        session_id = opened.data["id"]
        client.post(
            f"/api/v1/ventas/cash-sessions/{session_id}/close/",
            {"counted_closing_amount": "50.00"},
            format="json",
        )
        response = client.post(
            f"/api/v1/ventas/cash-sessions/{session_id}/close/",
            {"counted_closing_amount": "50.00"},
            format="json",
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["error"]["code"], "CASH_SESSION_NOT_OPEN")

    def test_cannot_add_movement_to_closed_session(self):
        client = self._client_as(self.admin_user)
        opened = client.post(
            "/api/v1/ventas/cash-sessions/open/",
            {"cash_register_id": self.cash_register.id, "opening_amount": "50.00"},
            format="json",
        )
        session_id = opened.data["id"]
        client.post(
            f"/api/v1/ventas/cash-sessions/{session_id}/close/",
            {"counted_closing_amount": "50.00"},
            format="json",
        )
        response = client.post(
            "/api/v1/ventas/cash-movements/",
            {
                "cash_session": session_id,
                "type": "IN",
                "concept": "AJUSTE",
                "amount": "10.00",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["error"]["code"], "CASH_SESSION_NOT_OPEN")

    def test_closing_session_writes_audit_log(self):
        from usuarios.models import AuditLog

        client = self._client_as(self.admin_user)
        opened = client.post(
            "/api/v1/ventas/cash-sessions/open/",
            {"cash_register_id": self.cash_register.id, "opening_amount": "50.00"},
            format="json",
        )
        session_id = opened.data["id"]
        client.post(
            f"/api/v1/ventas/cash-sessions/{session_id}/close/",
            {"counted_closing_amount": "50.00"},
            format="json",
        )
        self.assertTrue(
            AuditLog.objects.filter(
                action="CASH_SESSION_CLOSED", entity_id=session_id
            ).exists()
        )

    def test_movements_filtered_by_session(self):
        client = self._client_as(self.admin_user)
        other_register = CashRegister.objects.create(
            warehouse=self.warehouse, name="Caja 2"
        )
        session_a = CashSession.objects.create(
            cash_register=self.cash_register,
            user=self.admin_user,
            opening_amount="0",
            opening_at="2026-01-01T00:00:00Z",
            status="OPEN",
        )
        session_b = CashSession.objects.create(
            cash_register=other_register,
            user=self.admin_user,
            opening_amount="0",
            opening_at="2026-01-01T00:00:00Z",
            status="OPEN",
        )
        CashMovement.objects.create(
            cash_session=session_a,
            type="IN",
            concept="AJUSTE",
            amount="1.00",
            user=self.admin_user,
        )
        CashMovement.objects.create(
            cash_session=session_b,
            type="IN",
            concept="AJUSTE",
            amount="2.00",
            user=self.admin_user,
        )

        response = client.get(
            f"/api/v1/ventas/cash-movements/?cash_session={session_a.id}"
        )
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["amount"], "1.0000")

    def test_sessions_filtered_by_user_and_date_range(self):
        client = self._client_as(self.admin_user)
        other_admin = User.objects.create(
            email="admin2@negocio.com", role=self.admin_role
        )
        session_mine = CashSession.objects.create(
            cash_register=self.cash_register,
            user=self.admin_user,
            opening_amount="0",
            opening_at="2026-01-05T00:00:00Z",
            status="OPEN",
        )
        CashSession.objects.create(
            cash_register=self.cash_register,
            user=other_admin,
            opening_amount="0",
            opening_at="2026-01-05T00:00:00Z",
            status="CLOSED",
        )
        CashSession.objects.create(
            cash_register=self.cash_register,
            user=self.admin_user,
            opening_amount="0",
            opening_at="2020-01-01T00:00:00Z",
            status="CLOSED",
        )

        response = client.get(
            "/api/v1/ventas/cash-sessions/",
            {
                "user": self.admin_user.id,
                "opening_from": "2026-01-01",
                "opening_to": "2026-01-31",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual([row["id"] for row in response.data], [session_mine.id])

    def test_session_retrieve_includes_movements(self):
        client = self._client_as(self.admin_user)
        opened = client.post(
            "/api/v1/ventas/cash-sessions/open/",
            {"cash_register_id": self.cash_register.id, "opening_amount": "50.00"},
            format="json",
        )
        session_id = opened.data["id"]
        client.post(
            "/api/v1/ventas/cash-movements/",
            {
                "cash_session": session_id,
                "type": "IN",
                "concept": "AJUSTE",
                "amount": "10.00",
                "reason": "Fondo extra",
            },
            format="json",
        )

        response = client.get(f"/api/v1/ventas/cash-sessions/{session_id}/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data["movements"]), 1)
        self.assertEqual(response.data["movements"][0]["reason"], "Fondo extra")

    def test_movement_accepts_reason_and_receipt_url(self):
        client = self._client_as(self.admin_user)
        opened = client.post(
            "/api/v1/ventas/cash-sessions/open/",
            {"cash_register_id": self.cash_register.id, "opening_amount": "50.00"},
            format="json",
        )
        session_id = opened.data["id"]

        response = client.post(
            "/api/v1/ventas/cash-movements/",
            {
                "cash_session": session_id,
                "type": "OUT",
                "concept": "RETIRO",
                "amount": "10.00",
                "reason": "Pago de flete",
                "receipt_url": "https://bucket.s3.amazonaws.com/cash-movement-receipts/x.jpg",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["reason"], "Pago de flete")
        self.assertEqual(
            response.data["receipt_url"],
            "https://bucket.s3.amazonaws.com/cash-movement-receipts/x.jpg",
        )

    @override_settings(AWS_STORAGE_BUCKET_NAME="fivuza-test-bucket")
    @mock.patch.dict(
        os.environ,
        {"AWS_ACCESS_KEY_ID": "test", "AWS_SECRET_ACCESS_KEY": "test"},
    )
    def test_upload_receipt_url_returns_presigned_url(self):
        client = self._client_as(self.admin_user)
        response = client.post(
            "/api/v1/ventas/cash-movements/upload-receipt-url/",
            {"content_type": "image/jpeg"},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("upload_url", response.data)
        self.assertTrue(
            response.data["receipt_url"].startswith("https://")
            and "cash-movement-receipts/" in response.data["receipt_url"]
        )

    def test_upload_receipt_url_rejects_unsupported_content_type(self):
        client = self._client_as(self.admin_user)
        response = client.post(
            "/api/v1/ventas/cash-movements/upload-receipt-url/",
            {"content_type": "application/zip"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)

    def test_seller_cannot_request_receipt_upload_url(self):
        client = self._client_as(self.seller_user)
        response = client.post(
            "/api/v1/ventas/cash-movements/upload-receipt-url/",
            {"content_type": "image/jpeg"},
            format="json",
        )
        self.assertEqual(response.status_code, 403)

    def test_close_session_sends_alert_when_difference_exceeds_threshold(self):
        settings_row = TenantSettings.objects.get(tenant=self.tenant)
        settings_row.cash_difference_alert_threshold = "5.00"
        settings_row.save(update_fields=["cash_difference_alert_threshold"])

        client = self._client_as(self.admin_user)
        opened = client.post(
            "/api/v1/ventas/cash-sessions/open/",
            {"cash_register_id": self.cash_register.id, "opening_amount": "50.00"},
            format="json",
        )
        session_id = opened.data["id"]

        mail.outbox.clear()
        response = client.post(
            f"/api/v1/ventas/cash-sessions/{session_id}/close/",
            {"counted_closing_amount": "20.00"},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn(self.admin_user.email, mail.outbox[0].to)

    def test_close_session_no_alert_when_difference_within_threshold(self):
        settings_row = TenantSettings.objects.get(tenant=self.tenant)
        settings_row.cash_difference_alert_threshold = "5.00"
        settings_row.save(update_fields=["cash_difference_alert_threshold"])

        client = self._client_as(self.admin_user)
        opened = client.post(
            "/api/v1/ventas/cash-sessions/open/",
            {"cash_register_id": self.cash_register.id, "opening_amount": "50.00"},
            format="json",
        )
        session_id = opened.data["id"]

        mail.outbox.clear()
        response = client.post(
            f"/api/v1/ventas/cash-sessions/{session_id}/close/",
            {"counted_closing_amount": "50.00"},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(mail.outbox), 0)

    def test_cash_module_disabled_blocks_access(self):
        from core.models import TenantSettings

        settings = TenantSettings.objects.get(tenant=self.tenant)
        settings.cash_module_enabled = False
        settings.save(update_fields=["cash_module_enabled"])
        try:
            response = self._client_as(self.admin_user).get(
                "/api/v1/ventas/cash-sessions/"
            )
            self.assertEqual(response.status_code, 403)
            self.assertEqual(response.data["code"], "MODULE_DISABLED")
        finally:
            settings.cash_module_enabled = True
            settings.save(update_fields=["cash_module_enabled"])


class SalesCatalogEndpointsTests(TenantTestCase):
    """Clientes y promociones (Sprint 14): CRUD, busqueda y permisos."""

    @classmethod
    def get_test_schema_name(cls):
        return "test_ventas_catalogo"

    @classmethod
    def get_test_tenant_domain(cls):
        return "test-ventas-catalogo.test.com"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.password = "ClaveSegura123"
        cls.admin_role = Role.objects.get(name="admin")
        cls.seller_role = Role.objects.get(name="seller")
        cls.no_sales_role = Role.objects.create(name="auditor")

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

        cls.auditor_user = User.objects.create(
            email="auditor@negocio.com", role=cls.no_sales_role
        )
        cls.auditor_user.set_password(cls.password)
        cls.auditor_user.save()

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

    def test_seller_can_create_and_search_customer(self):
        client = self._client_as(self.seller_user)
        response = client.post(
            "/api/v1/ventas/customers/",
            {
                "document_type": "DNI",
                "document_number": "12345678",
                "name": "Juan Perez",
                "phone": "987654321",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201)

        by_document = client.get("/api/v1/ventas/customers/?search=12345678")
        self.assertEqual(len(by_document.data), 1)

        by_name = client.get("/api/v1/ventas/customers/?search=Perez")
        self.assertEqual(len(by_name.data), 1)

        by_phone = client.get("/api/v1/ventas/customers/?search=987654")
        self.assertEqual(len(by_phone.data), 1)

        no_match = client.get("/api/v1/ventas/customers/?search=nadie")
        self.assertEqual(len(no_match.data), 0)

    def test_auditor_without_sales_manage_cannot_create_customer(self):
        client = self._client_as(self.auditor_user)
        response = client.post(
            "/api/v1/ventas/customers/",
            {
                "document_type": "DNI",
                "document_number": "87654321",
                "name": "Sin Permiso",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 403)

    def test_auditor_can_still_read_customers(self):
        Customer.objects.create(
            document_type="DNI", document_number="11111111", name="Lectura Libre"
        )
        client = self._client_as(self.auditor_user)
        response = client.get("/api/v1/ventas/customers/")
        self.assertEqual(response.status_code, 200)

    def test_deleted_customer_excluded_from_list(self):
        client = self._client_as(self.admin_user)
        created = client.post(
            "/api/v1/ventas/customers/",
            {
                "document_type": "DNI",
                "document_number": "22222222",
                "name": "Cliente Borrado",
            },
            format="json",
        )
        customer_id = created.data["id"]

        delete_response = client.delete(f"/api/v1/ventas/customers/{customer_id}/")
        self.assertEqual(delete_response.status_code, 204)

        list_response = client.get("/api/v1/ventas/customers/")
        self.assertNotIn(customer_id, [row["id"] for row in list_response.data])

    def test_promotion_crud_with_targets(self):
        client = self._client_as(self.admin_user)
        created = client.post(
            "/api/v1/ventas/promotions/",
            {
                "name": "Descuento verano",
                "type": "PERCENTAGE",
                "value": "15.00",
                "start_date": "2026-01-01T00:00:00Z",
                "end_date": "2026-12-31T23:59:59Z",
            },
            format="json",
        )
        self.assertEqual(created.status_code, 201)
        promotion_id = created.data["id"]

        detail = client.get(f"/api/v1/ventas/promotions/{promotion_id}/")
        self.assertEqual(detail.data["targets"], [])

    def test_promotion_product_requires_exactly_one_target(self):
        client = self._client_as(self.admin_user)
        promotion = Promotion.objects.create(
            name="Promo",
            type="FIXED_AMOUNT",
            value="5.00",
            start_date="2026-01-01T00:00:00Z",
            end_date="2026-12-31T23:59:59Z",
        )
        response = client.post(
            "/api/v1/ventas/promotion-products/",
            {"promotion": promotion.id},
            format="json",
        )
        self.assertEqual(response.status_code, 400)

    def test_seller_cannot_manage_promotion_products_without_write_access(self):
        client = self._client_as(self.auditor_user)
        promotion = Promotion.objects.create(
            name="Promo",
            type="FIXED_AMOUNT",
            value="5.00",
            start_date="2026-01-01T00:00:00Z",
            end_date="2026-12-31T23:59:59Z",
        )
        response = client.get(
            f"/api/v1/ventas/promotion-products/?promotion={promotion.id}"
        )
        self.assertEqual(response.status_code, 403)


class SaleEndpointsTests(TenantTestCase):
    """POST /ventas/sales/ (SaleService.create_sale) y su listado/detalle
    (Sprint 15, API Spec §4.1, §2.3)."""

    @classmethod
    def get_test_schema_name(cls):
        return "test_ventas_sales_endpoints"

    @classmethod
    def get_test_tenant_domain(cls):
        return "test-ventas-sales-endpoints.test.com"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.password = "ClaveSegura123"
        cls.admin_role = Role.objects.get(name="admin")
        cls.seller_role = Role.objects.get(name="seller")
        cls.no_sales_role = Role.objects.create(name="auditor")

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

        cls.auditor_user = User.objects.create(
            email="auditor@negocio.com", role=cls.no_sales_role
        )
        cls.auditor_user.set_password(cls.password)
        cls.auditor_user.save()

        cls.warehouse = Warehouse.objects.create(name="Principal")
        category = Category.objects.create(name="Ropa")
        product = ProductVariantService.create_product(
            product_data={
                "type": "PRODUCT",
                "name": "Camiseta",
                "category": category,
                "unit_of_measure": "UND",
            },
            variants_data=[{"sku": "SALE-ENDPOINT-SKU", "price": "20.00"}],
        )
        cls.variant = product.variants.first()
        StockService.adjust_stock(
            variant=cls.variant,
            warehouse=cls.warehouse,
            counted_quantity=10,
            concept="ADJUSTMENT",
            user=cls.admin_user,
        )
        cls.customer = Customer.objects.create(
            document_type="DNI", document_number="55555555", name="Cliente Endpoint"
        )

    @classmethod
    def tearDownClass(cls):
        TenantSettings.objects.filter(tenant=cls.tenant).delete()
        super().tearDownClass()

    _register_counter = 0

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

    def _open_session(self, client=None):
        # Siempre se abre con el admin -un seller no tiene CASH_MANAGE
        # (decision deliberada desde Sprint 12), asi que no podria abrir su
        # propia caja aunque si pueda vender contra una ya abierta. Cada
        # test usa su propio CashRegister porque CashSessionService no
        # permite dos sesiones abiertas sobre el mismo registro.
        SaleEndpointsTests._register_counter += 1
        register = CashRegister.objects.create(
            warehouse=self.warehouse,
            name=f"Caja {SaleEndpointsTests._register_counter}",
        )
        response = self._client_as(self.admin_user).post(
            "/api/v1/ventas/cash-sessions/open/",
            {"cash_register_id": register.id, "opening_amount": "0"},
            format="json",
        )
        return response.data["id"]

    def test_seller_can_create_sale(self):
        client = self._client_as(self.seller_user)
        session_id = self._open_session(client)

        response = client.post(
            "/api/v1/ventas/sales/",
            {
                "customer_id": self.customer.id,
                "cash_session_id": session_id,
                "lines": [{"variant_id": self.variant.id, "quantity": "2"}],
                "payments": [{"method": "CASH", "amount": "40.00"}],
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["status"], "COMPLETED")
        self.assertEqual(response.data["total"], "40.0000")
        self.assertEqual(len(response.data["details"]), 1)

    def test_sale_rejects_payment_mismatch(self):
        client = self._client_as(self.admin_user)
        session_id = self._open_session(client)

        response = client.post(
            "/api/v1/ventas/sales/",
            {
                "customer_id": self.customer.id,
                "cash_session_id": session_id,
                "lines": [{"variant_id": self.variant.id, "quantity": "1"}],
                "payments": [{"method": "CASH", "amount": "5.00"}],
            },
            format="json",
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["error"]["code"], "PAYMENT_MISMATCH")

    def test_auditor_cannot_create_sale(self):
        client = self._client_as(self.auditor_user)
        response = client.post(
            "/api/v1/ventas/sales/",
            {
                "customer_id": self.customer.id,
                "cash_session_id": 1,
                "lines": [{"variant_id": self.variant.id, "quantity": "1"}],
                "payments": [{"method": "CASH", "amount": "20.00"}],
            },
            format="json",
        )
        self.assertEqual(response.status_code, 403)

    def test_auditor_can_read_sales(self):
        client = self._client_as(self.auditor_user)
        response = client.get("/api/v1/ventas/sales/")
        self.assertEqual(response.status_code, 200)

    def test_sales_filtered_by_customer(self):
        client = self._client_as(self.admin_user)
        session_id = self._open_session(client)
        client.post(
            "/api/v1/ventas/sales/",
            {
                "customer_id": self.customer.id,
                "cash_session_id": session_id,
                "lines": [{"variant_id": self.variant.id, "quantity": "1"}],
                "payments": [{"method": "CASH", "amount": "20.00"}],
            },
            format="json",
        )
        other_customer = Customer.objects.create(
            document_type="DNI", document_number="66666666", name="Otro Cliente"
        )

        response = client.get(f"/api/v1/ventas/sales/?customer={self.customer.id}")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(len(response.data) >= 1)
        self.assertTrue(
            all(row["customer"] == self.customer.id for row in response.data)
        )

        response_other = client.get(
            f"/api/v1/ventas/sales/?customer={other_customer.id}"
        )
        self.assertEqual(response_other.data, [])
