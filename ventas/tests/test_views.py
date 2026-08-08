# Pruebas de ViewSets/vistas: permisos, apertura/cierre de caja, arqueo.
import os
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.core import mail
from django.core.cache import cache
from django.db.models import Q, Sum
from django.test import override_settings
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase
from rest_framework.test import APIClient

from core.models import TenantSettings
from inventario.models import Category, Warehouse
from inventario.services import ProductVariantService, StockService
from usuarios.models import Role, User
from ventas.models import (
    CashMovement,
    CashRegister,
    CashSession,
    Customer,
    Promotion,
    Sale,
)


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

    def test_pos_catalog_returns_variant_with_stock_and_price(self):
        client = self._client_as(self.seller_user)
        response = client.get(
            f"/api/v1/ventas/pos/catalog/?warehouse={self.warehouse.id}"
        )
        self.assertEqual(response.status_code, 200)
        row = next(row for row in response.data if row["id"] == self.variant.id)
        self.assertEqual(row["sku"], self.variant.sku)
        self.assertEqual(row["stock"], "10.000")

    def test_pos_search_by_sku(self):
        client = self._client_as(self.seller_user)
        response = client.get(
            f"/api/v1/ventas/pos/search/?warehouse={self.warehouse.id}&q={self.variant.sku}"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual([row["id"] for row in response.data], [self.variant.id])

    def test_pos_search_without_query_returns_empty_list(self):
        client = self._client_as(self.seller_user)
        response = client.get(
            f"/api/v1/ventas/pos/search/?warehouse={self.warehouse.id}"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, [])

    def _create_sale(self, client):
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
        return response.data["id"]

    def test_receipt_returns_fixed_width_html(self):
        self.tenant.company_name = "Bodega Lucho"
        self.tenant.ruc = "20123456789"
        self.tenant.save()

        client = self._client_as(self.seller_user)
        sale_id = self._create_sale(client)

        response = client.get(f"/api/v1/ventas/sales/{sale_id}/receipt/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/html", response["Content-Type"])
        body = response.content.decode()
        self.assertIn("Bodega Lucho", body)
        self.assertIn("RUC: 20123456789", body)
        self.assertIn("2 x Camiseta", body)
        self.assertIn("TOTAL: S/. 40.00", body)
        self.assertIn("CASH: 40.00", body)
        self.assertIn("width:58mm", body)

    def test_receipt_accepts_80mm_width(self):
        client = self._client_as(self.seller_user)
        sale_id = self._create_sale(client)

        response = client.get(f"/api/v1/ventas/sales/{sale_id}/receipt/?width_mm=80")
        self.assertEqual(response.status_code, 200)
        self.assertIn("width:80mm", response.content.decode())

    def test_receipt_not_found_returns_404_with_envelope(self):
        client = self._client_as(self.seller_user)
        response = client.get("/api/v1/ventas/sales/999999/receipt/")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.data["error"]["code"], "NOT_FOUND")

    def test_receipt_shows_blank_instead_of_none_when_tenant_has_no_ruc(self):
        self.tenant.ruc = None
        self.tenant.save()

        client = self._client_as(self.seller_user)
        sale_id = self._create_sale(client)

        response = client.get(f"/api/v1/ventas/sales/{sale_id}/receipt/")
        body = response.content.decode()
        self.assertIn("RUC: ", body)
        self.assertNotIn("None", body)

    def test_auditor_can_view_receipt(self):
        client = self._client_as(self.admin_user)
        sale_id = self._create_sale(client)

        response = self._client_as(self.auditor_user).get(
            f"/api/v1/ventas/sales/{sale_id}/receipt/"
        )
        self.assertEqual(response.status_code, 200)

    def test_pos_catalog_requires_sales_module_access(self):
        client = self._client_as(self.auditor_user)
        response = client.get(
            f"/api/v1/ventas/pos/catalog/?warehouse={self.warehouse.id}"
        )
        # El auditor si tiene acceso de lectura al modulo de ventas (mismo
        # esquema que customers/promotions): solo escritura requiere
        # SALES_MANAGE, lectura esta abierta a cualquier tenant.users.
        self.assertEqual(response.status_code, 200)


class SaleVoidAndReturnTests(TenantTestCase):
    """POST /ventas/sales/{id}/void/ y /ventas/sale-returns/ (Sprint 18,
    Plan de Implementacion: los dos reversos de una venta)."""

    @classmethod
    def get_test_schema_name(cls):
        return "test_ventas_void_return"

    @classmethod
    def get_test_tenant_domain(cls):
        return "test-ventas-void-return.test.com"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.password = "ClaveSegura123"
        cls.admin_role = Role.objects.get(name="admin")
        cls.seller_role = Role.objects.get(name="seller")
        cls.auditor_role = Role.objects.create(name="auditor")

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
            email="auditor@negocio.com", role=cls.auditor_role
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
            variants_data=[{"sku": "VOID-RETURN-SKU", "price": "20.00"}],
        )
        cls.variant = product.variants.first()
        cls.customer = Customer.objects.create(
            document_type="DNI", document_number="77777777", name="Cliente Reversos"
        )

    @classmethod
    def tearDownClass(cls):
        TenantSettings.objects.filter(tenant=cls.tenant).delete()
        super().tearDownClass()

    _register_counter = 0

    def setUp(self):
        cache.clear()
        StockService.adjust_stock(
            variant=self.variant,
            warehouse=self.warehouse,
            counted_quantity=10,
            concept="ADJUSTMENT",
            user=self.admin_user,
        )

    def _client_as(self, user):
        client = APIClient(HTTP_HOST=self.domain.domain)
        login = client.post(
            "/api/v1/auth/login/",
            {"email": user.email, "password": self.password},
            format="json",
        )
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {login.data['access']}")
        return client

    def _open_session(self):
        SaleVoidAndReturnTests._register_counter += 1
        register = CashRegister.objects.create(
            warehouse=self.warehouse,
            name=f"Caja {SaleVoidAndReturnTests._register_counter}",
        )
        response = self._client_as(self.admin_user).post(
            "/api/v1/ventas/cash-sessions/open/",
            {"cash_register_id": register.id, "opening_amount": "0"},
            format="json",
        )
        return response.data["id"]

    def _create_sale(self, client, session_id, quantity="2", method="CASH"):
        response = client.post(
            "/api/v1/ventas/sales/",
            {
                "customer_id": self.customer.id,
                "cash_session_id": session_id,
                "lines": [{"variant_id": self.variant.id, "quantity": quantity}],
                "payments": [
                    {
                        "method": method,
                        "amount": str(Decimal(quantity) * Decimal("20.00")),
                    }
                ],
            },
            format="json",
        )
        return response.data

    def _stock_quantity(self):
        from inventario.models import Stock

        return Stock.objects.get(
            variant=self.variant, warehouse=self.warehouse
        ).quantity

    def test_admin_can_void_sale_and_restocks(self):
        client = self._client_as(self.admin_user)
        session_id = self._open_session()
        sale = self._create_sale(client, session_id)
        self.assertEqual(self._stock_quantity(), Decimal("8"))

        response = client.post(
            f"/api/v1/ventas/sales/{sale['id']}/void/",
            {"reason": "Venta duplicada por error"},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], "VOIDED")
        self.assertEqual(self._stock_quantity(), Decimal("10"))

    def test_void_reverses_cash_in_session(self):
        client = self._client_as(self.admin_user)
        session_id = self._open_session()
        sale = self._create_sale(client, session_id)

        client.post(
            f"/api/v1/ventas/sales/{sale['id']}/void/",
            {"reason": "Error de cobro"},
            format="json",
        )

        movements = client.get(
            f"/api/v1/ventas/cash-movements/?cash_session={session_id}"
        )
        devolucion = next(m for m in movements.data if m["concept"] == "DEVOLUCION")
        self.assertEqual(devolucion["type"], "OUT")
        self.assertEqual(devolucion["amount"], "40.0000")

    def test_seller_cannot_void_sale(self):
        admin_client = self._client_as(self.admin_user)
        session_id = self._open_session()
        sale = self._create_sale(admin_client, session_id)

        response = self._client_as(self.seller_user).post(
            f"/api/v1/ventas/sales/{sale['id']}/void/",
            {"reason": "Intento no autorizado"},
            format="json",
        )
        self.assertEqual(response.status_code, 403)

    def test_cannot_void_sale_with_returns(self):
        client = self._client_as(self.admin_user)
        session_id = self._open_session()
        sale = self._create_sale(client, session_id)
        detail_id = sale["details"][0]["id"]

        client.post(
            "/api/v1/ventas/sale-returns/",
            {
                "sale_id": sale["id"],
                "reason": "Producto defectuoso",
                "refund_type": "BALANCE",
                "items": [{"sale_detail_id": detail_id, "quantity_returned": "1"}],
            },
            format="json",
        )

        response = client.post(
            f"/api/v1/ventas/sales/{sale['id']}/void/",
            {"reason": "Ya no aplica"},
            format="json",
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["error"]["code"], "SALE_HAS_RETURNS")

    def test_cannot_void_sale_after_cash_session_closed(self):
        client = self._client_as(self.admin_user)
        session_id = self._open_session()
        sale = self._create_sale(client, session_id)

        client.post(
            f"/api/v1/ventas/cash-sessions/{session_id}/close/",
            {"counted_closing_amount": "40.00"},
            format="json",
        )

        response = client.post(
            f"/api/v1/ventas/sales/{sale['id']}/void/",
            {"reason": "Demasiado tarde"},
            format="json",
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["error"]["code"], "CASH_SESSION_CLOSED")

    def test_partial_return_restocks_and_generates_balance(self):
        client = self._client_as(self.admin_user)
        session_id = self._open_session()
        sale = self._create_sale(client, session_id, quantity="4")
        detail_id = sale["details"][0]["id"]
        self.assertEqual(self._stock_quantity(), Decimal("6"))

        response = client.post(
            "/api/v1/ventas/sale-returns/",
            {
                "sale_id": sale["id"],
                "reason": "Le sobraron 2",
                "refund_type": "BALANCE",
                "items": [{"sale_detail_id": detail_id, "quantity_returned": "2"}],
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["total_refund_amount"], "40.0000")
        self.assertEqual(self._stock_quantity(), Decimal("8"))

        from ventas.models import CustomerBalanceLedger

        ledger_entry = CustomerBalanceLedger.objects.get(
            sale_return_id=response.data["id"]
        )
        self.assertEqual(ledger_entry.type, "CREDIT")
        self.assertEqual(ledger_entry.amount, Decimal("40.0000"))

    def test_return_with_cash_refund_generates_cash_movement(self):
        client = self._client_as(self.seller_user)
        session_id = self._open_session()
        sale = self._create_sale(client, session_id)
        detail_id = sale["details"][0]["id"]

        response = client.post(
            "/api/v1/ventas/sale-returns/",
            {
                "sale_id": sale["id"],
                "reason": "No le gusto",
                "refund_type": "CASH",
                "cash_session_id": session_id,
                "items": [{"sale_detail_id": detail_id, "quantity_returned": "1"}],
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201)

        movements = client.get(
            f"/api/v1/ventas/cash-movements/?cash_session={session_id}"
        )
        devolucion = next(m for m in movements.data if m["concept"] == "DEVOLUCION")
        self.assertEqual(devolucion["amount"], "20.0000")

    def test_cannot_return_more_than_sold(self):
        client = self._client_as(self.admin_user)
        session_id = self._open_session()
        sale = self._create_sale(client, session_id, quantity="2")
        detail_id = sale["details"][0]["id"]

        response = client.post(
            "/api/v1/ventas/sale-returns/",
            {
                "sale_id": sale["id"],
                "reason": "Excede lo vendido",
                "refund_type": "BALANCE",
                "items": [{"sale_detail_id": detail_id, "quantity_returned": "3"}],
            },
            format="json",
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["error"]["code"], "RETURN_EXCEEDS_SOLD")

    def test_two_returns_on_same_sale_track_already_returned(self):
        client = self._client_as(self.admin_user)
        session_id = self._open_session()
        sale = self._create_sale(client, session_id, quantity="4")
        detail_id = sale["details"][0]["id"]

        first = client.post(
            "/api/v1/ventas/sale-returns/",
            {
                "sale_id": sale["id"],
                "reason": "Primera tanda",
                "refund_type": "BALANCE",
                "items": [{"sale_detail_id": detail_id, "quantity_returned": "3"}],
            },
            format="json",
        )
        self.assertEqual(first.status_code, 201)

        second_ok = client.post(
            "/api/v1/ventas/sale-returns/",
            {
                "sale_id": sale["id"],
                "reason": "Segunda tanda, lo que queda",
                "refund_type": "BALANCE",
                "items": [{"sale_detail_id": detail_id, "quantity_returned": "1"}],
            },
            format="json",
        )
        self.assertEqual(second_ok.status_code, 201)

        third_fails = client.post(
            "/api/v1/ventas/sale-returns/",
            {
                "sale_id": sale["id"],
                "reason": "Ya no queda nada que devolver",
                "refund_type": "BALANCE",
                "items": [{"sale_detail_id": detail_id, "quantity_returned": "1"}],
            },
            format="json",
        )
        self.assertEqual(third_fails.status_code, 409)
        self.assertEqual(third_fails.data["error"]["code"], "RETURN_EXCEEDS_SOLD")

    def test_auditor_cannot_create_return(self):
        client = self._client_as(self.admin_user)
        session_id = self._open_session()
        sale = self._create_sale(client, session_id)
        detail_id = sale["details"][0]["id"]

        response = self._client_as(self.auditor_user).post(
            "/api/v1/ventas/sale-returns/",
            {
                "sale_id": sale["id"],
                "reason": "Sin permiso",
                "refund_type": "BALANCE",
                "items": [{"sale_detail_id": detail_id, "quantity_returned": "1"}],
            },
            format="json",
        )
        self.assertEqual(response.status_code, 403)


class CreditLedgerTests(TenantTestCase):
    """CreditLedgerService: credito/fiado y saldo a favor (Sprint 18,
    Plan de Implementacion)."""

    @classmethod
    def get_test_schema_name(cls):
        return "test_ventas_credit_ledger"

    @classmethod
    def get_test_tenant_domain(cls):
        return "test-ventas-credit-ledger.test.com"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.password = "ClaveSegura123"
        cls.admin_role = Role.objects.get(name="admin")
        cls.seller_role = Role.objects.get(name="seller")
        cls.auditor_role = Role.objects.create(name="auditor")

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
            email="auditor@negocio.com", role=cls.auditor_role
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
            variants_data=[{"sku": "CREDIT-SKU", "price": "20.00"}],
        )
        cls.variant = product.variants.first()

    @classmethod
    def tearDownClass(cls):
        TenantSettings.objects.filter(tenant=cls.tenant).delete()
        super().tearDownClass()

    _register_counter = 0

    def setUp(self):
        cache.clear()
        StockService.adjust_stock(
            variant=self.variant,
            warehouse=self.warehouse,
            counted_quantity=20,
            concept="ADJUSTMENT",
            user=self.admin_user,
        )
        self.customer = Customer.objects.create(
            document_type="DNI",
            document_number=f"9{CreditLedgerTests._register_counter}",
            name="Cliente Fiado",
        )

    def _client_as(self, user):
        client = APIClient(HTTP_HOST=self.domain.domain)
        login = client.post(
            "/api/v1/auth/login/",
            {"email": user.email, "password": self.password},
            format="json",
        )
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {login.data['access']}")
        return client

    def _open_session(self):
        CreditLedgerTests._register_counter += 1
        register = CashRegister.objects.create(
            warehouse=self.warehouse,
            name=f"Caja {CreditLedgerTests._register_counter}",
        )
        response = self._client_as(self.admin_user).post(
            "/api/v1/ventas/cash-sessions/open/",
            {"cash_register_id": register.id, "opening_amount": "0"},
            format="json",
        )
        return response.data["id"]

    def _create_sale(self, client, session_id, quantity="1", method="CREDIT_LEDGER"):
        return client.post(
            "/api/v1/ventas/sales/",
            {
                "customer_id": self.customer.id,
                "cash_session_id": session_id,
                "lines": [{"variant_id": self.variant.id, "quantity": quantity}],
                "payments": [
                    {
                        "method": method,
                        "amount": str(Decimal(quantity) * Decimal("20.00")),
                    }
                ],
            },
            format="json",
        )

    def test_credit_sale_writes_debt(self):
        client = self._client_as(self.admin_user)
        session_id = self._open_session()
        response = self._create_sale(client, session_id, quantity="2")
        self.assertEqual(response.status_code, 201)

        customer_response = client.get(f"/api/v1/ventas/customers/{self.customer.id}/")
        self.assertEqual(customer_response.data["current_debt"], "40.0000")

    def test_credit_sale_blocked_by_credit_limit(self):
        self.customer.credit_limit = Decimal("30.00")
        self.customer.save()

        client = self._client_as(self.admin_user)
        session_id = self._open_session()
        response = self._create_sale(client, session_id, quantity="2")

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["error"]["code"], "CREDIT_LIMIT_EXCEEDED")

    def test_credit_sale_within_limit_succeeds(self):
        self.customer.credit_limit = Decimal("100.00")
        self.customer.save()

        client = self._client_as(self.admin_user)
        session_id = self._open_session()
        response = self._create_sale(client, session_id, quantity="2")
        self.assertEqual(response.status_code, 201)

    def test_register_payment_reduces_debt(self):
        client = self._client_as(self.admin_user)
        session_id = self._open_session()
        self._create_sale(client, session_id, quantity="2")

        response = client.post(
            "/api/v1/ventas/customer-debt-ledger/register-payment/",
            {
                "customer_id": self.customer.id,
                "amount": "15.00",
                "description": "Abono",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["customer_current_debt"], "25.0000")

    def test_balance_payment_consumes_and_depletes_balance(self):
        client = self._client_as(self.admin_user)
        session_id = self._open_session()

        # Genera saldo a favor: venta al contado + devolucion con refund_type=BALANCE.
        cash_sale = self._create_sale(client, session_id, quantity="2", method="CASH")
        detail_id = cash_sale.data["details"][0]["id"]
        client.post(
            "/api/v1/ventas/sale-returns/",
            {
                "sale_id": cash_sale.data["id"],
                "reason": "Devolucion para generar saldo",
                "refund_type": "BALANCE",
                "items": [{"sale_detail_id": detail_id, "quantity_returned": "2"}],
            },
            format="json",
        )
        customer_response = client.get(f"/api/v1/ventas/customers/{self.customer.id}/")
        self.assertEqual(customer_response.data["current_balance"], "40.0000")

        # Paga otra venta con ese saldo.
        balance_sale = self._create_sale(
            client, session_id, quantity="1", method="BALANCE"
        )
        self.assertEqual(balance_sale.status_code, 201)

        customer_response = client.get(f"/api/v1/ventas/customers/{self.customer.id}/")
        self.assertEqual(customer_response.data["current_balance"], "20.0000")

    def test_balance_payment_fails_if_insufficient(self):
        client = self._client_as(self.admin_user)
        session_id = self._open_session()

        response = self._create_sale(client, session_id, quantity="1", method="BALANCE")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["error"]["code"], "INSUFFICIENT_BALANCE")

    def test_voiding_credit_sale_reverses_debt(self):
        client = self._client_as(self.admin_user)
        session_id = self._open_session()
        sale = self._create_sale(client, session_id, quantity="2").data

        client.post(
            f"/api/v1/ventas/sales/{sale['id']}/void/",
            {"reason": "Error de cobro"},
            format="json",
        )
        customer_response = client.get(f"/api/v1/ventas/customers/{self.customer.id}/")
        self.assertEqual(customer_response.data["current_debt"], "0.0000")

    def test_ledger_sum_matches_reported_balance_after_mixed_operations(self):
        from ventas.models import CustomerBalanceLedger, CustomerDebtLedger
        from ventas.services import CreditLedgerService

        client = self._client_as(self.admin_user)
        session_id = self._open_session()

        # Venta a credito, abono parcial, venta al contado devuelta a saldo,
        # y esa venta a credito anulada -todo intercalado.
        credit_sale = self._create_sale(client, session_id, quantity="3").data
        client.post(
            "/api/v1/ventas/customer-debt-ledger/register-payment/",
            {"customer_id": self.customer.id, "amount": "10.00"},
            format="json",
        )
        cash_sale = self._create_sale(
            client, session_id, quantity="1", method="CASH"
        ).data
        client.post(
            "/api/v1/ventas/sale-returns/",
            {
                "sale_id": cash_sale["id"],
                "refund_type": "BALANCE",
                "items": [
                    {
                        "sale_detail_id": cash_sale["details"][0]["id"],
                        "quantity_returned": "1",
                    }
                ],
            },
            format="json",
        )
        client.post(
            f"/api/v1/ventas/sales/{credit_sale['id']}/void/",
            {"reason": "Cliente se arrepintio"},
            format="json",
        )

        expected_debt = CustomerDebtLedger.objects.filter(
            customer=self.customer
        ).aggregate(
            debit=Sum("amount", filter=Q(type="DEBIT")),
            credit=Sum("amount", filter=Q(type="CREDIT")),
        )
        expected_balance = CustomerBalanceLedger.objects.filter(
            customer=self.customer
        ).aggregate(
            credit=Sum("amount", filter=Q(type="CREDIT")),
            debit=Sum("amount", filter=Q(type="DEBIT")),
        )
        self.assertEqual(
            CreditLedgerService.get_debt(self.customer),
            (expected_debt["debit"] or Decimal("0"))
            - (expected_debt["credit"] or Decimal("0")),
        )
        self.assertEqual(
            CreditLedgerService.get_balance(self.customer),
            (expected_balance["credit"] or Decimal("0"))
            - (expected_balance["debit"] or Decimal("0")),
        )

    def test_auditor_cannot_register_payment(self):
        response = self._client_as(self.auditor_user).post(
            "/api/v1/ventas/customer-debt-ledger/register-payment/",
            {"customer_id": self.customer.id, "amount": "10.00"},
            format="json",
        )
        self.assertEqual(response.status_code, 403)


class SaleSyncTests(TenantTestCase):
    """POST /ventas/sales/sync/ (Sprint 20, API Spec §4.2): sincronizacion
    en lote de ventas offline, idempotente por client_side_uuid."""

    @classmethod
    def get_test_schema_name(cls):
        return "test_ventas_sale_sync"

    @classmethod
    def get_test_tenant_domain(cls):
        return "test-ventas-sale-sync.test.com"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.password = "ClaveSegura123"
        cls.admin_role = Role.objects.get(name="admin")
        cls.auditor_role = Role.objects.create(name="auditor")

        cls.admin_user = User.objects.create(
            email="admin@negocio.com", role=cls.admin_role
        )
        cls.admin_user.set_password(cls.password)
        cls.admin_user.save()

        cls.auditor_user = User.objects.create(
            email="auditor@negocio.com", role=cls.auditor_role
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
            variants_data=[{"sku": "SYNC-SKU", "price": "20.00"}],
        )
        cls.variant = product.variants.first()
        cls.customer = Customer.objects.create(
            document_type="DNI", document_number="66666666", name="Cliente Sync"
        )

    @classmethod
    def tearDownClass(cls):
        TenantSettings.objects.filter(tenant=cls.tenant).delete()
        super().tearDownClass()

    _register_counter = 0

    def setUp(self):
        cache.clear()
        StockService.adjust_stock(
            variant=self.variant,
            warehouse=self.warehouse,
            counted_quantity=5,
            concept="ADJUSTMENT",
            user=self.admin_user,
        )

    def _client_as(self, user):
        client = APIClient(HTTP_HOST=self.domain.domain)
        login = client.post(
            "/api/v1/auth/login/",
            {"email": user.email, "password": self.password},
            format="json",
        )
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {login.data['access']}")
        return client

    def _open_session(self):
        SaleSyncTests._register_counter += 1
        register = CashRegister.objects.create(
            warehouse=self.warehouse,
            name=f"Caja Sync {SaleSyncTests._register_counter}",
        )
        response = self._client_as(self.admin_user).post(
            "/api/v1/ventas/cash-sessions/open/",
            {"cash_register_id": register.id, "opening_amount": "0"},
            format="json",
        )
        return response.data["id"]

    def _sale_payload(self, uuid_value, session_id, quantity="1"):
        return {
            "client_side_uuid": uuid_value,
            "customer_id": self.customer.id,
            "cash_session_id": session_id,
            "lines": [{"variant_id": self.variant.id, "quantity": quantity}],
            "payments": [
                {"method": "CASH", "amount": str(Decimal(quantity) * Decimal("20.00"))}
            ],
        }

    def _stock_quantity(self):
        from inventario.models import Stock

        return Stock.objects.get(
            variant=self.variant, warehouse=self.warehouse
        ).quantity

    def test_sync_creates_new_sales(self):
        client = self._client_as(self.admin_user)
        session_id = self._open_session()

        response = client.post(
            "/api/v1/ventas/sales/sync/",
            {
                "sales": [
                    self._sale_payload("uuid-sync-1", session_id),
                    self._sale_payload("uuid-sync-2", session_id),
                ]
            },
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        statuses = {
            row["client_side_uuid"]: row["status"] for row in response.data["synced"]
        }
        self.assertEqual(statuses, {"uuid-sync-1": "CREATED", "uuid-sync-2": "CREATED"})
        self.assertEqual(response.data["conflicts"], [])
        self.assertEqual(
            Sale.objects.filter(client_side_uuid__startswith="uuid-sync-").count(), 2
        )

    def test_sync_is_idempotent_when_same_batch_sent_three_times(self):
        client = self._client_as(self.admin_user)
        session_id = self._open_session()
        payload = {"sales": [self._sale_payload("uuid-repeat", session_id)]}

        for _ in range(3):
            response = client.post("/api/v1/ventas/sales/sync/", payload, format="json")
            self.assertEqual(response.status_code, 200)

        self.assertEqual(Sale.objects.filter(client_side_uuid="uuid-repeat").count(), 1)
        # El stock solo se descuenta una vez -si el reenvio hubiera vuelto a
        # procesar la venta, quedaria en 3 en vez de 4.
        self.assertEqual(self._stock_quantity(), Decimal("4"))

    def test_sync_mixed_batch_new_and_duplicate(self):
        client = self._client_as(self.admin_user)
        session_id = self._open_session()

        client.post(
            "/api/v1/ventas/sales/sync/",
            {"sales": [self._sale_payload("uuid-first", session_id)]},
            format="json",
        )

        response = client.post(
            "/api/v1/ventas/sales/sync/",
            {
                "sales": [
                    self._sale_payload("uuid-first", session_id),
                    self._sale_payload("uuid-second", session_id),
                ]
            },
            format="json",
        )
        statuses = {
            row["client_side_uuid"]: row["status"] for row in response.data["synced"]
        }
        self.assertEqual(
            statuses, {"uuid-first": "DUPLICATE_IGNORED", "uuid-second": "CREATED"}
        )

    def test_sync_allows_oversell_and_flags_movement(self):
        from inventario.models import InventoryMovement

        client = self._client_as(self.admin_user)
        session_id = self._open_session()

        # Solo hay 5 en stock; la venta offline pide 8 -el producto ya salio
        # fisicamente cuando el cajero la cobro sin conexion.
        response = client.post(
            "/api/v1/ventas/sales/sync/",
            {"sales": [self._sale_payload("uuid-oversell", session_id, quantity="8")]},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["synced"][0]["status"], "CREATED")
        self.assertEqual(len(response.data["conflicts"]), 1)
        self.assertEqual(response.data["conflicts"][0]["variant_id"], self.variant.id)
        self.assertTrue(response.data["conflicts"][0]["oversell_flag"])

        sale = Sale.objects.get(client_side_uuid="uuid-oversell")
        movement = InventoryMovement.objects.filter(
            variant=self.variant, concept="SALE"
        ).latest("created_at")
        self.assertTrue(movement.oversell_flag)
        self.assertEqual(self._stock_quantity(), Decimal("-3"))
        self.assertEqual(sale.status, "COMPLETED")

    def test_sync_does_not_reject_via_normal_endpoint_semantics(self):
        # Confirma que el endpoint normal (no offline) SIGUE rechazando la
        # sobreventa -allow_oversell solo se activa desde el sync.
        client = self._client_as(self.admin_user)
        session_id = self._open_session()

        response = client.post(
            "/api/v1/ventas/sales/",
            {
                "customer_id": self.customer.id,
                "cash_session_id": session_id,
                "lines": [{"variant_id": self.variant.id, "quantity": "8"}],
                "payments": [{"method": "CASH", "amount": "160.00"}],
            },
            format="json",
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["error"]["code"], "INSUFFICIENT_STOCK")

    def test_auditor_cannot_sync(self):
        response = self._client_as(self.auditor_user).post(
            "/api/v1/ventas/sales/sync/",
            {"sales": [self._sale_payload("uuid-forbidden", "1")]},
            format="json",
        )
        self.assertEqual(response.status_code, 403)

    def test_sync_rejects_empty_batch(self):
        response = self._client_as(self.admin_user).post(
            "/api/v1/ventas/sales/sync/", {"sales": []}, format="json"
        )
        self.assertEqual(response.status_code, 400)

    def test_sync_two_devices_same_variant_offline_flags_the_second(self):
        # Dos tablets vendiendo la misma variante sin conexion, cada una sin
        # saber lo que la otra vendio -al sincronizar juntas en el mismo
        # lote, sync_batch() las procesa en orden y solo la que ya no
        # alcanza queda marcada con oversell_flag (Sprint 21, TRD §7.2).
        client = self._client_as(self.admin_user)
        session_id = self._open_session()

        response = client.post(
            "/api/v1/ventas/sales/sync/",
            {
                "sales": [
                    self._sale_payload("uuid-device-a", session_id, quantity="3"),
                    self._sale_payload("uuid-device-b", session_id, quantity="3"),
                ]
            },
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        statuses = {
            row["client_side_uuid"]: row["status"] for row in response.data["synced"]
        }
        self.assertEqual(
            statuses, {"uuid-device-a": "CREATED", "uuid-device-b": "CREATED"}
        )
        conflict_uuids = {row["client_side_uuid"] for row in response.data["conflicts"]}
        self.assertEqual(conflict_uuids, {"uuid-device-b"})
        # 5 - 3 - 3 = -1: ambas ventas se registraron, ninguna se perdio.
        self.assertEqual(self._stock_quantity(), Decimal("-1"))

    def test_sync_large_batch_of_over_hundred_sales_deduplicates_correctly(self):
        # Cola con volumen alto (100+ ventas acumuladas durante un corte
        # largo, Sprint 21 TRD §7.2). Se reenvia el mismo lote dos veces
        # -como haria el cliente si la respuesta de la primera sincronizacion
        # se perdio a mitad de camino- y no debe duplicar ninguna.
        client = self._client_as(self.admin_user)
        session_id = self._open_session()
        StockService.adjust_stock(
            variant=self.variant,
            warehouse=self.warehouse,
            counted_quantity=1000,
            concept="ADJUSTMENT",
            user=self.admin_user,
        )
        payload = {
            "sales": [
                self._sale_payload(f"uuid-bulk-{i}", session_id) for i in range(120)
            ]
        }

        first = client.post("/api/v1/ventas/sales/sync/", payload, format="json")
        self.assertEqual(first.status_code, 200)
        first_statuses = [row["status"] for row in first.data["synced"]]
        self.assertEqual(first_statuses, ["CREATED"] * 120)

        second = client.post("/api/v1/ventas/sales/sync/", payload, format="json")
        self.assertEqual(second.status_code, 200)
        second_statuses = [row["status"] for row in second.data["synced"]]
        self.assertEqual(second_statuses, ["DUPLICATE_IGNORED"] * 120)

        self.assertEqual(
            Sale.objects.filter(client_side_uuid__startswith="uuid-bulk-").count(), 120
        )


class SalesReportTests(TenantTestCase):
    """GET /ventas/reports/sales/ (Sprint 24, API Spec §4.16): misma
    consulta para pantalla y exportacion, via ReportExportService."""

    @classmethod
    def get_test_schema_name(cls):
        return "test_ventas_sales_report"

    @classmethod
    def get_test_tenant_domain(cls):
        return "test-ventas-sales-report.test.com"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.password = "ClaveSegura123"
        cls.admin_role = Role.objects.get(name="admin")
        cls.admin_user = User.objects.create(
            email="admin@negocio.com", role=cls.admin_role
        )
        cls.admin_user.set_password(cls.password)
        cls.admin_user.save()

        cls.warehouse = Warehouse.objects.create(name="Principal")
        category = Category.objects.create(name="Ropa")
        product = ProductVariantService.create_product(
            product_data={
                "type": "PRODUCT",
                "name": "Camiseta",
                "category": category,
                "unit_of_measure": "UND",
            },
            variants_data=[{"sku": "REPORT-SKU", "price": "20.00"}],
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
            document_type="DNI", document_number="66666666", name="Cliente Reporte"
        )

    @classmethod
    def tearDownClass(cls):
        TenantSettings.objects.filter(tenant=cls.tenant).delete()
        super().tearDownClass()

    def setUp(self):
        cache.clear()

    def _client(self):
        client = APIClient(HTTP_HOST=self.domain.domain)
        login = client.post(
            "/api/v1/auth/login/",
            {"email": self.admin_user.email, "password": self.password},
            format="json",
        )
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {login.data['access']}")
        return client

    def test_sales_report_includes_created_sale(self):
        client = self._client()
        register = CashRegister.objects.create(
            warehouse=self.warehouse, name="Caja reporte"
        )
        session = client.post(
            "/api/v1/ventas/cash-sessions/open/",
            {"cash_register_id": register.id, "opening_amount": "0"},
            format="json",
        )
        client.post(
            "/api/v1/ventas/sales/",
            {
                "customer_id": self.customer.id,
                "cash_session_id": session.data["id"],
                "lines": [{"variant_id": self.variant.id, "quantity": "1"}],
                "payments": [{"method": "CASH", "amount": "20.00"}],
            },
            format="json",
        )

        today = timezone.localdate().isoformat()
        response = client.get(
            f"/api/v1/ventas/reports/sales/?date_from={today}&date_to={today}"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["customer"], "Cliente Reporte")

    def test_sales_report_csv_export(self):
        client = self._client()
        today = timezone.localdate().isoformat()
        response = client.get(
            f"/api/v1/ventas/reports/sales/?date_from={today}&date_to={today}&export=csv"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/csv")


class CashReportTests(TenantTestCase):
    """GET /ventas/reports/cash-sessions/ y /ventas/reports/cash-movements/
    (Sprint 25, API Spec §4.16)."""

    @classmethod
    def get_test_schema_name(cls):
        return "test_ventas_cash_reports"

    @classmethod
    def get_test_tenant_domain(cls):
        return "test-ventas-cash-reports.test.com"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.password = "ClaveSegura123"
        cls.admin_role = Role.objects.get(name="admin")
        cls.admin_user = User.objects.create(
            email="admin@negocio.com", role=cls.admin_role
        )
        cls.admin_user.set_password(cls.password)
        cls.admin_user.save()

        cls.warehouse = Warehouse.objects.create(name="Principal")
        cls.cash_register = CashRegister.objects.create(
            warehouse=cls.warehouse, name="Caja reportes"
        )

    @classmethod
    def tearDownClass(cls):
        TenantSettings.objects.filter(tenant=cls.tenant).delete()
        super().tearDownClass()

    def setUp(self):
        cache.clear()

    def _client(self):
        client = APIClient(HTTP_HOST=self.domain.domain)
        login = client.post(
            "/api/v1/auth/login/",
            {"email": self.admin_user.email, "password": self.password},
            format="json",
        )
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {login.data['access']}")
        return client

    def _open_and_close_session(self, client):
        opened = client.post(
            "/api/v1/ventas/cash-sessions/open/",
            {"cash_register_id": self.cash_register.id, "opening_amount": "50.00"},
            format="json",
        )
        session_id = opened.data["id"]
        movement = client.post(
            "/api/v1/ventas/cash-movements/",
            {
                "cash_session": session_id,
                "type": "OUT",
                "concept": "RETIRO",
                "amount": "10.00",
                "reason": "Prueba",
            },
            format="json",
        )
        assert movement.status_code == 201, movement.data
        client.post(
            f"/api/v1/ventas/cash-sessions/{session_id}/close/",
            {"counted_closing_amount": "40.00"},
            format="json",
        )
        return session_id

    def test_cash_session_report_includes_closed_session(self):
        client = self._client()
        self._open_and_close_session(client)

        today = timezone.localdate().isoformat()
        response = client.get(
            f"/api/v1/ventas/reports/cash-sessions/?date_from={today}&date_to={today}"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["status"], "CLOSED")

    def test_cash_session_report_xlsx_export(self):
        today = timezone.localdate().isoformat()
        response = self._client().get(
            f"/api/v1/ventas/reports/cash-sessions/?date_from={today}&date_to={today}&export=xlsx"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response["Content-Type"],
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    def test_cash_movement_report_includes_manual_movement(self):
        client = self._client()
        self._open_and_close_session(client)

        today = timezone.localdate().isoformat()
        response = client.get(
            f"/api/v1/ventas/reports/cash-movements/?date_from={today}&date_to={today}"
        )
        self.assertEqual(response.status_code, 200)
        concepts = [row["concept"] for row in response.data]
        self.assertIn("RETIRO", concepts)

    def test_cash_movement_report_csv_export(self):
        today = timezone.localdate().isoformat()
        response = self._client().get(
            f"/api/v1/ventas/reports/cash-movements/?date_from={today}&date_to={today}&export=csv"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/csv")


class ReservationEndpointsTests(TenantTestCase):
    """CRUD y acciones convert/cancel de /ventas/reservations/ (Sprint 28,
    Ficha de Producto §5.2)."""

    @classmethod
    def get_test_schema_name(cls):
        return "test_ventas_reservation_endpoints"

    @classmethod
    def get_test_tenant_domain(cls):
        return "test-ventas-reservation-endpoints.test.com"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.password = "ClaveSegura123"
        cls.admin_user = User.objects.create(
            email="admin@negocio.com", role=Role.objects.get(name="admin")
        )
        cls.admin_user.set_password(cls.password)
        cls.admin_user.save()

        cls.warehouse = Warehouse.objects.create(name="Principal")
        category = Category.objects.create(name="Ropa")
        product = ProductVariantService.create_product(
            product_data={
                "type": "PRODUCT",
                "name": "Camiseta",
                "category": category,
                "unit_of_measure": "UND",
            },
            variants_data=[{"sku": "RESERVA-ENDPOINT-SKU", "price": "20.00"}],
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
            document_type="DNI", document_number="66666666", name="Cliente Reserva"
        )

    @classmethod
    def tearDownClass(cls):
        TenantSettings.objects.filter(tenant=cls.tenant).delete()
        super().tearDownClass()

    def setUp(self):
        cache.clear()

    def _client(self):
        client = APIClient(HTTP_HOST=self.domain.domain)
        login = client.post(
            "/api/v1/auth/login/",
            {"email": self.admin_user.email, "password": self.password},
            format="json",
        )
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {login.data['access']}")
        return client

    def _open_session(self, client):
        register = CashRegister.objects.create(
            warehouse=self.warehouse, name=f"Caja {timezone.now().timestamp()}"
        )
        response = client.post(
            "/api/v1/ventas/cash-sessions/open/",
            {"cash_register_id": register.id, "opening_amount": "0"},
            format="json",
        )
        return response.data["id"]

    def test_create_and_list_reservation(self):
        client = self._client()
        response = client.post(
            "/api/v1/ventas/reservations/",
            {
                "customer_id": self.customer.id,
                "variant_id": self.variant.id,
                "warehouse_id": self.warehouse.id,
                "quantity": "3",
                "expires_at": (timezone.now() + timedelta(days=1)).isoformat(),
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["status"], "ACTIVE")

        listing = client.get("/api/v1/ventas/reservations/?status=ACTIVE")
        self.assertEqual(listing.status_code, 200)
        self.assertEqual(len(listing.data), 1)

    def test_reservation_exceeding_stock_returns_409(self):
        client = self._client()
        response = client.post(
            "/api/v1/ventas/reservations/",
            {
                "customer_id": self.customer.id,
                "variant_id": self.variant.id,
                "warehouse_id": self.warehouse.id,
                "quantity": "50",
                "expires_at": (timezone.now() + timedelta(days=1)).isoformat(),
            },
            format="json",
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["error"]["code"], "INSUFFICIENT_STOCK")

    def test_convert_reservation_creates_sale(self):
        client = self._client()
        create_response = client.post(
            "/api/v1/ventas/reservations/",
            {
                "customer_id": self.customer.id,
                "variant_id": self.variant.id,
                "warehouse_id": self.warehouse.id,
                "quantity": "2",
                "expires_at": (timezone.now() + timedelta(days=1)).isoformat(),
            },
            format="json",
        )
        reservation_id = create_response.data["id"]
        session_id = self._open_session(client)

        response = client.post(
            f"/api/v1/ventas/reservations/{reservation_id}/convert/",
            {
                "cash_session_id": session_id,
                "payments": [{"method": "CASH", "amount": "40.00"}],
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["status"], "COMPLETED")

    def test_cancel_reservation(self):
        client = self._client()
        create_response = client.post(
            "/api/v1/ventas/reservations/",
            {
                "customer_id": self.customer.id,
                "variant_id": self.variant.id,
                "warehouse_id": self.warehouse.id,
                "quantity": "2",
                "expires_at": (timezone.now() + timedelta(days=1)).isoformat(),
            },
            format="json",
        )
        reservation_id = create_response.data["id"]

        response = client.post(f"/api/v1/ventas/reservations/{reservation_id}/cancel/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], "CANCELLED")


class QuoteEndpointsTests(TenantTestCase):
    """CRUD y acciones de estado/convert de /ventas/quotes/ (Sprint 28,
    Ficha de Producto §5.2)."""

    @classmethod
    def get_test_schema_name(cls):
        return "test_ventas_quote_endpoints"

    @classmethod
    def get_test_tenant_domain(cls):
        return "test-ventas-quote-endpoints.test.com"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.password = "ClaveSegura123"
        cls.admin_user = User.objects.create(
            email="admin@negocio.com", role=Role.objects.get(name="admin")
        )
        cls.admin_user.set_password(cls.password)
        cls.admin_user.save()

        cls.warehouse = Warehouse.objects.create(name="Principal")
        category = Category.objects.create(name="Ropa")
        product = ProductVariantService.create_product(
            product_data={
                "type": "PRODUCT",
                "name": "Camiseta",
                "category": category,
                "unit_of_measure": "UND",
            },
            variants_data=[{"sku": "COTIZA-ENDPOINT-SKU", "price": "20.00"}],
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
            document_type="DNI", document_number="77777777", name="Cliente Cotizacion"
        )

    @classmethod
    def tearDownClass(cls):
        TenantSettings.objects.filter(tenant=cls.tenant).delete()
        super().tearDownClass()

    def setUp(self):
        cache.clear()

    def _client(self):
        client = APIClient(HTTP_HOST=self.domain.domain)
        login = client.post(
            "/api/v1/auth/login/",
            {"email": self.admin_user.email, "password": self.password},
            format="json",
        )
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {login.data['access']}")
        return client

    def _open_session(self, client):
        register = CashRegister.objects.create(
            warehouse=self.warehouse, name=f"Caja {timezone.now().timestamp()}"
        )
        response = client.post(
            "/api/v1/ventas/cash-sessions/open/",
            {"cash_register_id": register.id, "opening_amount": "0"},
            format="json",
        )
        return response.data["id"]

    def _create_quote(self, client):
        return client.post(
            "/api/v1/ventas/quotes/",
            {
                "customer_id": self.customer.id,
                "valid_until": (timezone.now() + timedelta(days=7)).isoformat(),
                "lines": [{"variant_id": self.variant.id, "quantity": "2"}],
            },
            format="json",
        )

    def test_create_quote_freezes_price(self):
        client = self._client()
        response = self._create_quote(client)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["status"], "DRAFT")
        self.assertEqual(response.data["total"], "40.0000")
        self.assertEqual(response.data["details"][0]["unit_price"], "20.0000")

    def test_quote_document_returns_html(self):
        client = self._client()
        quote_id = self._create_quote(client).data["id"]
        response = client.get(f"/api/v1/ventas/quotes/{quote_id}/document/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/html")

    def test_full_lifecycle_sent_accepted_convert(self):
        client = self._client()
        quote_id = self._create_quote(client).data["id"]

        sent = client.post(f"/api/v1/ventas/quotes/{quote_id}/mark-sent/")
        self.assertEqual(sent.data["status"], "SENT")

        accepted = client.post(f"/api/v1/ventas/quotes/{quote_id}/mark-accepted/")
        self.assertEqual(accepted.data["status"], "ACCEPTED")

        session_id = self._open_session(client)
        response = client.post(
            f"/api/v1/ventas/quotes/{quote_id}/convert/",
            {
                "cash_session_id": session_id,
                "payments": [{"method": "CASH", "amount": "40.00"}],
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["total"], "40.0000")

    def test_convert_quote_not_accepted_returns_409(self):
        client = self._client()
        quote_id = self._create_quote(client).data["id"]
        session_id = self._open_session(client)

        response = client.post(
            f"/api/v1/ventas/quotes/{quote_id}/convert/",
            {
                "cash_session_id": session_id,
                "payments": [{"method": "CASH", "amount": "40.00"}],
            },
            format="json",
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["error"]["code"], "QUOTE_NOT_ACCEPTED")

    def test_mark_rejected(self):
        client = self._client()
        quote_id = self._create_quote(client).data["id"]
        response = client.post(f"/api/v1/ventas/quotes/{quote_id}/mark-rejected/")
        self.assertEqual(response.data["status"], "REJECTED")
