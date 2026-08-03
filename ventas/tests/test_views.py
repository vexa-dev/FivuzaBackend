# Pruebas de ViewSets/vistas: permisos, apertura/cierre de caja, arqueo.
from django.core.cache import cache
from django_tenants.test.cases import TenantTestCase
from rest_framework.test import APIClient

from core.models import TenantSettings
from inventario.models import Warehouse
from usuarios.models import Role, User
from ventas.models import CashMovement, CashRegister, CashSession


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
