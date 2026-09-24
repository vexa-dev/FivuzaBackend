# Bloque A (Plan de Mejoras Operativas): control de caja y permisos.
# Cubre los permisos separados de apertura/cierre (A.1), la caja asignada y
# el dueño del turno (A.2), el arqueo a ciegas (A.3) y el desglose por
# metodo de pago (A.4).
from decimal import Decimal
from unittest import mock

from django.core.cache import cache
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase
from rest_framework.test import APIClient

from core.models import TenantSettings
from inventario.models import Category, Warehouse
from inventario.services import ProductVariantService, StockService
from usuarios.models import (
    AuditLog,
    Permission,
    Role,
    RolePermission,
    User,
    UserWarehouse,
)
from usuarios.services import PermissionService
from ventas.models import CashRegister, CashSession, Customer
from ventas.services import CashSessionService, NoCashSessionError, SaleService
from ventas.services.cash import CashSessionNotOwnedError


class CashOwnershipTests(TenantTestCase):
    @classmethod
    def get_test_schema_name(cls):
        return "test_ventas_cash_ownership"

    @classmethod
    def get_test_tenant_domain(cls):
        return "test-ventas-cash-ownership.test.com"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.password = "ClaveSegura123"
        cls.admin_role = Role.objects.get(name="admin")
        cls.seller_role = Role.objects.get(name="seller")

        cls.admin_user = cls._create_user("admin@negocio.com", cls.admin_role)
        cls.seller_one = cls._create_user("cajero1@negocio.com", cls.seller_role)
        cls.seller_two = cls._create_user("cajero2@negocio.com", cls.seller_role)

        # Rol heredado: solo CASH_MANAGE, como quedaron los tenants que ya
        # estaban en marcha antes de este bloque.
        cls.legacy_role = Role.objects.create(name="cajero_legacy")
        for code in ("CASH_MANAGE", "SALES_MANAGE", "INVENTORY_VIEW"):
            RolePermission.objects.create(
                role=cls.legacy_role, permission=Permission.objects.get(code=code)
            )
        cls.legacy_user = cls._create_user("legacy@negocio.com", cls.legacy_role)

        cls.warehouse = Warehouse.objects.create(name="Principal")
        for user in (cls.seller_one, cls.seller_two, cls.legacy_user):
            UserWarehouse.objects.create(user=user, warehouse=cls.warehouse)

        cls.free_register = CashRegister.objects.create(
            warehouse=cls.warehouse, name="Caja libre"
        )
        cls.assigned_register = CashRegister.objects.create(
            warehouse=cls.warehouse,
            name="Caja de cajero1",
            assigned_user=cls.seller_one,
        )
        cls.category = Category.objects.create(name="Ropa")
        cls.customer = Customer.objects.create(
            document_type="DNI", document_number="11111111", name="Cliente Uno"
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
        CashSession.objects.all().delete()
        self._set_switches(can_open=False, can_close=False)

    def _set_switches(self, *, can_open, can_close):
        TenantSettings.objects.filter(tenant=self.tenant).update(
            cashier_can_open_session=can_open, cashier_can_close_session=can_close
        )
        PermissionService.invalidate_cashier_switches_cache()

    def _client_as(self, user):
        client = APIClient(HTTP_HOST=self.domain.domain)
        login = client.post(
            "/api/v1/auth/login/",
            {"email": user.email, "password": self.password},
            format="json",
        )
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {login.data['access']}")
        return client

    def _open_session(self, *, register=None, user=None):
        return CashSession.objects.create(
            cash_register=register or self.free_register,
            user=user or self.seller_one,
            opening_amount=Decimal("0"),
            opening_at=timezone.now(),
            status="OPEN",
        )

    _sku_counter = 0

    def _create_variant(self, *, price="20.00", stock_quantity="10"):
        CashOwnershipTests._sku_counter += 1
        product = ProductVariantService.create_product(
            product_data={
                "type": "PRODUCT",
                "name": "Camiseta",
                "category": self.category,
                "unit_of_measure": "UND",
            },
            variants_data=[
                {"sku": f"SKU-CASH-A-{CashOwnershipTests._sku_counter}", "price": price}
            ],
        )
        variant = product.variants.first()
        StockService.adjust_stock(
            variant=variant,
            warehouse=self.warehouse,
            counted_quantity=Decimal(stock_quantity),
            concept="ADJUSTMENT",
            user=self.admin_user,
        )
        return variant

    # --- A.1: permisos separados de apertura y cierre --------------------

    def test_cashier_cannot_open_while_switch_is_off(self):
        response = self._client_as(self.seller_one).post(
            "/api/v1/ventas/cash-sessions/open/",
            {"cash_register_id": self.free_register.id, "opening_amount": "10.00"},
            format="json",
        )
        self.assertEqual(response.status_code, 403)

    def test_switch_lets_cashier_open_without_touching_his_role(self):
        self._set_switches(can_open=True, can_close=False)
        response = self._client_as(self.seller_one).post(
            "/api/v1/ventas/cash-sessions/open/",
            {"cash_register_id": self.free_register.id, "opening_amount": "10.00"},
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        self.assertNotIn(
            "CASH_OPEN", PermissionService.get_permission_codes(self.seller_one)
        )

    def test_open_switch_does_not_grant_close_nor_submit(self):
        self._set_switches(can_open=True, can_close=False)
        session = self._open_session()
        client = self._client_as(self.seller_one)
        self.assertEqual(
            client.post(
                f"/api/v1/ventas/cash-sessions/{session.id}/close/",
                {"counted_closing_amount": "10.00"},
                format="json",
            ).status_code,
            403,
        )
        self.assertEqual(
            client.post(
                f"/api/v1/ventas/cash-sessions/{session.id}/submit-count/",
                {"counted_closing_amount": "10.00"},
                format="json",
            ).status_code,
            403,
        )

    def test_close_switch_never_grants_the_final_close(self):
        """El interruptor concede CASH_SUBMIT_COUNT, no CASH_CLOSE: el cajero
        entrega su caja, el supervisor la cierra."""
        self._set_switches(can_open=True, can_close=True)
        codes = PermissionService.get_effective_permission_codes(self.seller_one)
        self.assertIn("CASH_SUBMIT_COUNT", codes)
        self.assertNotIn("CASH_CLOSE", codes)

        session = self._open_session()
        response = self._client_as(self.seller_one).post(
            f"/api/v1/ventas/cash-sessions/{session.id}/close/",
            {"counted_closing_amount": "10.00"},
            format="json",
        )
        self.assertEqual(response.status_code, 403)

    def test_cashier_with_the_switch_is_not_a_supervisor(self):
        """Con el interruptor encendido, el cajero sigue sin poder tocar la
        caja de otro ni verla en su listado."""
        self._set_switches(can_open=True, can_close=True)
        other = self._open_session(register=self.free_register, user=self.seller_two)

        client = self._client_as(self.seller_one)
        response = client.post(
            f"/api/v1/ventas/cash-sessions/{other.id}/submit-count/",
            {"counted_closing_amount": "10.00"},
            format="json",
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data["error"]["code"], "CASH_SESSION_NOT_OWNED")

        listed = client.get("/api/v1/ventas/cash-sessions/")
        self.assertNotIn(other.id, [row["id"] for row in listed.data["results"]])

    def test_turning_the_switch_off_takes_effect_immediately(self):
        self._set_switches(can_open=True, can_close=False)
        client = self._client_as(self.seller_one)
        opened = client.post(
            "/api/v1/ventas/cash-sessions/open/",
            {"cash_register_id": self.free_register.id, "opening_amount": "10.00"},
            format="json",
        )
        self.assertEqual(opened.status_code, 201)
        CashSession.objects.all().delete()

        self._set_switches(can_open=False, can_close=False)
        blocked = client.post(
            "/api/v1/ventas/cash-sessions/open/",
            {"cash_register_id": self.free_register.id, "opening_amount": "10.00"},
            format="json",
        )
        self.assertEqual(blocked.status_code, 403)

    def test_cash_manage_still_implies_open_and_close(self):
        """Compatibilidad hacia atras: un rol que solo tenia CASH_MANAGE
        sigue abriendo y cerrando sin que nadie le toque los permisos."""
        codes = PermissionService.get_permission_codes(self.legacy_user)
        self.assertIn("CASH_OPEN", codes)
        self.assertIn("CASH_CLOSE", codes)

        client = self._client_as(self.legacy_user)
        opened = client.post(
            "/api/v1/ventas/cash-sessions/open/",
            {"cash_register_id": self.free_register.id, "opening_amount": "10.00"},
            format="json",
        )
        self.assertEqual(opened.status_code, 201)
        closed = client.post(
            "/api/v1/ventas/cash-sessions/%s/close/" % opened.data["id"],
            {"counted_closing_amount": "10.00"},
            format="json",
        )
        self.assertEqual(closed.status_code, 200)

    # --- A.2: caja asignada y dueño del turno ----------------------------

    def test_assigned_register_rejects_another_cashier(self):
        self._set_switches(can_open=True, can_close=False)
        response = self._client_as(self.seller_two).post(
            "/api/v1/ventas/cash-sessions/open/",
            {"cash_register_id": self.assigned_register.id, "opening_amount": "10.00"},
            format="json",
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data["error"]["code"], "CASH_SESSION_NOT_OWNED")

    def test_assigned_cashier_opens_his_own_register(self):
        self._set_switches(can_open=True, can_close=False)
        response = self._client_as(self.seller_one).post(
            "/api/v1/ventas/cash-sessions/open/",
            {"cash_register_id": self.assigned_register.id, "opening_amount": "10.00"},
            format="json",
        )
        self.assertEqual(response.status_code, 201)

    def test_cashier_cannot_sell_on_someone_elses_session(self):
        session = self._open_session(user=self.seller_one)
        variant = self._create_variant()
        with self.assertRaises(CashSessionNotOwnedError):
            SaleService.create_sale(
                customer=self.customer,
                cash_session=session,
                user=self.seller_two,
                lines=[{"variant_id": variant.id, "quantity": "1"}],
                payments=[{"method": "CASH", "amount": Decimal("20.00")}],
            )

    def test_session_owner_can_sell_on_his_own_session(self):
        session = self._open_session(user=self.seller_one)
        variant = self._create_variant()
        sale = SaleService.create_sale(
            customer=self.customer,
            cash_session=session,
            user=self.seller_one,
            lines=[{"variant_id": variant.id, "quantity": "1"}],
            payments=[{"method": "CASH", "amount": Decimal("20.00")}],
        )
        self.assertEqual(sale.cash_session_id, session.id)

    def test_supervisor_can_close_a_session_he_did_not_open(self):
        """El caso real: el cajero se fue sin cerrar."""
        session = self._open_session(user=self.seller_one)
        response = self._client_as(self.admin_user).post(
            f"/api/v1/ventas/cash-sessions/{session.id}/close/",
            {"counted_closing_amount": "0.00"},
            format="json",
        )
        self.assertEqual(response.status_code, 200)

    def test_cashier_only_lists_sessions_he_can_operate(self):
        own = self._open_session(register=self.assigned_register, user=self.seller_one)
        other = self._open_session(register=self.free_register, user=self.seller_two)

        listed = self._client_as(self.seller_one).get("/api/v1/ventas/cash-sessions/")
        ids = [row["id"] for row in listed.data["results"]]
        self.assertIn(own.id, ids)
        self.assertNotIn(other.id, ids)

        supervisor = self._client_as(self.admin_user).get(
            "/api/v1/ventas/cash-sessions/"
        )
        supervisor_ids = [row["id"] for row in supervisor.data["results"]]
        self.assertIn(own.id, supervisor_ids)
        self.assertIn(other.id, supervisor_ids)

    # --- A.3: arqueo a ciegas --------------------------------------------

    def test_cashier_does_not_receive_the_expected_amount(self):
        self._set_switches(can_open=True, can_close=True)
        session = self._open_session(user=self.seller_one)
        detail = self._client_as(self.seller_one).get(
            f"/api/v1/ventas/cash-sessions/{session.id}/"
        )
        self.assertIsNone(detail.data["expected_amount_so_far"])

        supervisor = self._client_as(self.admin_user).get(
            f"/api/v1/ventas/cash-sessions/{session.id}/"
        )
        self.assertEqual(supervisor.data["expected_amount_so_far"], "0.00")

    def test_handing_over_the_till_hides_the_result_from_the_cashier(self):
        self._set_switches(can_open=True, can_close=True)
        session = self._open_session(user=self.seller_one)
        response = self._client_as(self.seller_one).post(
            f"/api/v1/ventas/cash-sessions/{session.id}/submit-count/",
            {"counted_closing_amount": "7.00"},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], "PENDING_APPROVAL")
        self.assertEqual(response.data["counted_closing_amount"], "7.00")
        self.assertIsNone(response.data["expected_closing_amount"])
        self.assertIsNone(response.data["difference"])
        self.assertIsNone(response.data["expected_amount_so_far"])

        # El supervisor si ve el esperado de la caja entregada: es lo que
        # necesita para aprobarla.
        supervisor = self._client_as(self.admin_user).get(
            f"/api/v1/ventas/cash-sessions/{session.id}/"
        )
        self.assertEqual(supervisor.data["expected_amount_so_far"], "0.00")

    # --- Cierre en dos pasos ---------------------------------------------

    def test_a_handed_over_till_takes_no_more_sales_or_movements(self):
        self._set_switches(can_open=True, can_close=True)
        session = self._open_session(user=self.seller_one)
        variant = self._create_variant()
        self._client_as(self.seller_one).post(
            f"/api/v1/ventas/cash-sessions/{session.id}/submit-count/",
            {"counted_closing_amount": "0.00"},
            format="json",
        )

        session.refresh_from_db()
        with self.assertRaises(NoCashSessionError):
            SaleService.create_sale(
                customer=self.customer,
                cash_session=session,
                user=self.seller_one,
                lines=[{"variant_id": variant.id, "quantity": "1"}],
                payments=[{"method": "CASH", "amount": Decimal("20.00")}],
            )

        movement = self._client_as(self.admin_user).post(
            "/api/v1/ventas/cash-movements/",
            {
                "cash_session": session.id,
                "type": "IN",
                "concept": "AJUSTE",
                "amount": "5.00",
            },
            format="json",
        )
        self.assertEqual(movement.status_code, 409)

    def test_a_new_shift_starts_while_the_previous_awaits_approval(self):
        """La caja entregada espera al supervisor, pero el mostrador no: el
        turno siguiente abre igual."""
        self._set_switches(can_open=True, can_close=True)
        session = self._open_session(
            register=self.assigned_register, user=self.seller_one
        )
        client = self._client_as(self.seller_one)
        client.post(
            f"/api/v1/ventas/cash-sessions/{session.id}/submit-count/",
            {"counted_closing_amount": "5.00"},
            format="json",
        )

        reopened = client.post(
            "/api/v1/ventas/cash-sessions/open/",
            {"cash_register_id": self.assigned_register.id, "opening_amount": "5.00"},
            format="json",
        )
        self.assertEqual(reopened.status_code, 201)

    def test_cannot_hand_over_the_same_till_twice(self):
        self._set_switches(can_open=True, can_close=True)
        session = self._open_session(user=self.seller_one)
        client = self._client_as(self.seller_one)
        client.post(
            f"/api/v1/ventas/cash-sessions/{session.id}/submit-count/",
            {"counted_closing_amount": "5.00"},
            format="json",
        )
        response = client.post(
            f"/api/v1/ventas/cash-sessions/{session.id}/submit-count/",
            {"counted_closing_amount": "9.00"},
            format="json",
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["error"]["code"], "CASH_SESSION_ALREADY_COUNTED")

    def test_supervisor_closes_a_handed_over_till_without_recounting(self):
        self._set_switches(can_open=True, can_close=True)
        session = self._open_session(user=self.seller_one)
        self._client_as(self.seller_one).post(
            f"/api/v1/ventas/cash-sessions/{session.id}/submit-count/",
            {"counted_closing_amount": "7.00"},
            format="json",
        )

        response = self._client_as(self.admin_user).post(
            f"/api/v1/ventas/cash-sessions/{session.id}/close/", {}, format="json"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], "CLOSED")
        self.assertEqual(response.data["counted_closing_amount"], "7.00")
        self.assertEqual(response.data["difference"], "7.00")

        session.refresh_from_db()
        self.assertEqual(session.approved_by_id, self.admin_user.id)
        self.assertEqual(session.user_id, self.seller_one.id)
        self.assertIsNotNone(session.counted_at)

    def test_handing_over_notifies_the_administrator(self):
        self._set_switches(can_open=True, can_close=True)
        session = self._open_session(user=self.seller_one)
        with mock.patch("ventas.tasks.send_cash_count_submitted_alert.delay") as notify:
            self._client_as(self.seller_one).post(
                f"/api/v1/ventas/cash-sessions/{session.id}/submit-count/",
                {"counted_closing_amount": "5.00"},
                format="json",
            )
        notify.assert_called_once()

    def test_closing_records_who_counted_and_who_approved(self):
        self._set_switches(can_open=True, can_close=True)
        session = self._open_session(user=self.seller_one)
        self._client_as(self.seller_one).post(
            f"/api/v1/ventas/cash-sessions/{session.id}/submit-count/",
            {"counted_closing_amount": "7.00"},
            format="json",
        )
        self._client_as(self.admin_user).post(
            f"/api/v1/ventas/cash-sessions/{session.id}/close/", {}, format="json"
        )

        log = AuditLog.objects.filter(
            action="CASH_SESSION_CLOSED", entity_id=session.id
        ).first()
        self.assertIsNotNone(log)
        self.assertIn(self.seller_one.email, log.details)
        self.assertIn(self.admin_user.email, log.details)

    def test_supervisor_still_closes_an_open_till_in_one_step(self):
        session = self._open_session(user=self.admin_user)
        response = self._client_as(self.admin_user).post(
            f"/api/v1/ventas/cash-sessions/{session.id}/close/",
            {"counted_closing_amount": "3.00"},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], "CLOSED")
        self.assertEqual(response.data["difference"], "3.00")

    def test_closing_an_open_till_without_an_amount_is_rejected(self):
        session = self._open_session(user=self.admin_user)
        response = self._client_as(self.admin_user).post(
            f"/api/v1/ventas/cash-sessions/{session.id}/close/", {}, format="json"
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["error"]["code"], "COUNTED_AMOUNT_REQUIRED")

    # --- A.4: desglose por metodo de pago ---------------------------------

    def test_session_detail_breaks_down_payments_by_method(self):
        session = self._open_session(user=self.seller_one)
        variant = self._create_variant(price="100.00", stock_quantity="10")
        SaleService.create_sale(
            customer=self.customer,
            cash_session=session,
            user=self.seller_one,
            lines=[{"variant_id": variant.id, "quantity": "1"}],
            payments=[
                {"method": "CASH", "amount": Decimal("50.00")},
                {"method": "CARD", "amount": Decimal("30.00")},
                {"method": "CREDIT_LEDGER", "amount": Decimal("20.00")},
            ],
        )

        totals = CashSessionService.payment_totals_by_method(session)
        self.assertEqual(totals["CASH"], "50.00")
        self.assertEqual(totals["CARD"], "30.00")
        self.assertEqual(totals["CREDIT_LEDGER"], "20.00")
        self.assertEqual(totals["YAPE"], "0")

        detail = self._client_as(self.admin_user).get(
            f"/api/v1/ventas/cash-sessions/{session.id}/"
        )
        self.assertEqual(detail.data["payment_totals"]["CASH"], "50.00")
        self.assertEqual(detail.data["payment_totals"]["CARD"], "30.00")
        self.assertEqual(detail.data["payment_totals"]["CREDIT_LEDGER"], "20.00")

    # --- Reporte de arqueo (A.3) -----------------------------------------

    def test_cashier_cannot_pull_the_cash_report(self):
        """El reporte lleva esperado y diferencia: sin el, el arqueo a
        ciegas se esquivaba bajando un Excel."""
        self._set_switches(can_open=True, can_close=True)
        response = self._client_as(self.seller_one).get(
            "/api/v1/ventas/reports/cash-sessions/",
            {"date_from": "2020-01-01", "date_to": "2030-01-01"},
        )
        self.assertEqual(response.status_code, 403)

    def test_cash_report_only_lists_closed_tills_by_default(self):
        open_session = self._open_session(user=self.admin_user)
        closed = self._open_session(
            register=self.assigned_register, user=self.seller_one
        )
        self._client_as(self.admin_user).post(
            f"/api/v1/ventas/cash-sessions/{closed.id}/close/",
            {"counted_closing_amount": "0.00"},
            format="json",
        )

        client = self._client_as(self.admin_user)
        params = {"date_from": "2020-01-01", "date_to": "2030-01-01"}
        default = client.get("/api/v1/ventas/reports/cash-sessions/", params)
        self.assertEqual({row["status"] for row in default.data}, {"CLOSED"})

        with_open = client.get(
            "/api/v1/ventas/reports/cash-sessions/",
            {**params, "include_open": "true"},
        )
        self.assertIn("OPEN", {row["status"] for row in with_open.data})
        self.assertEqual(len(with_open.data), 2)
        self.assertIsNotNone(open_session.id)

    def test_exporting_the_cash_report_is_audited(self):
        self._client_as(self.admin_user).get(
            "/api/v1/ventas/reports/cash-sessions/",
            {"date_from": "2020-01-01", "date_to": "2030-01-01", "export": "csv"},
        )
        self.assertTrue(
            AuditLog.objects.filter(action="CASH_SESSION_REPORT_EXPORTED").exists()
        )

    # --- Asignacion de cajas (A.2) ---------------------------------------

    def test_cannot_assign_a_till_to_someone_without_access_to_its_warehouse(self):
        outsider = self._create_user("ajeno@negocio.com", self.seller_role)
        response = self._client_as(self.admin_user).patch(
            f"/api/v1/ventas/cash-registers/{self.free_register.id}/",
            {"assigned_user": outsider.id},
            format="json",
        )
        self.assertEqual(response.status_code, 400)

    def test_cannot_assign_a_till_to_someone_inactive(self):
        retired = self._create_user("retirado@negocio.com", self.seller_role)
        UserWarehouse.objects.create(user=retired, warehouse=self.warehouse)
        retired.is_active = False
        retired.save(update_fields=["is_active"])

        response = self._client_as(self.admin_user).patch(
            f"/api/v1/ventas/cash-registers/{self.free_register.id}/",
            {"assigned_user": retired.id},
            format="json",
        )
        self.assertEqual(response.status_code, 400)

    def test_deactivating_a_cashier_frees_his_till(self):
        register = CashRegister.objects.create(
            warehouse=self.warehouse,
            name="Caja de quien se va",
            assigned_user=self.seller_two,
        )
        self.seller_two.is_active = False
        self.seller_two.save(update_fields=["is_active"])

        register.refresh_from_db()
        self.assertIsNone(register.assigned_user_id)

        self.seller_two.is_active = True
        self.seller_two.save(update_fields=["is_active"])
