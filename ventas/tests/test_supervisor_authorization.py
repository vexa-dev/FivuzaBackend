# Bloque C (Plan de Mejoras Operativas): autorizacion de supervisor de un
# solo uso (C.1), tope de descuento manual por rol (C.2) e intentos de acceso
# con correos desconocidos (C.4).
import json
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.core.cache import cache
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase
from rest_framework.test import APIClient

from core.models import TenantSettings
from inventario.models import Category, Warehouse
from inventario.services import ProductVariantService, StockService
from usuarios.authorization import LoginAttemptService
from usuarios.models import (
    AuditLog,
    LoginAttempt,
    Permission,
    Role,
    RolePermission,
    SupervisorAuthorization,
    User,
    UserWarehouse,
)
from ventas.models import (
    CashRegister,
    CashSession,
    Customer,
    Promotion,
    PromotionProduct,
    Quote,
    Sale,
)
from ventas.services import SaleService

_HEADER = "HTTP_X_SUPERVISOR_AUTHORIZATION"


class SupervisorAuthorizationTests(TenantTestCase):
    @classmethod
    def get_test_schema_name(cls):
        return "test_ventas_supervisor_auth"

    @classmethod
    def get_test_tenant_domain(cls):
        return "test-ventas-supervisor-auth.test.com"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.password = "ClaveSegura123"
        cls.seller_role = Role.objects.get(name="seller")
        cls.admin = cls._create_user(
            "admin@negocio.com", Role.objects.get(name="admin")
        )
        cls.manager = cls._create_user(
            "supervisor@negocio.com", Role.objects.get(name="manager")
        )
        cls.cashier = cls._create_user("cajero@negocio.com", cls.seller_role)
        cls.other_cashier = cls._create_user("cajero2@negocio.com", cls.seller_role)

        # Un rol a medida que vende pero no devuelve: la devolucion necesita
        # autorizacion (el seller por defecto si tiene SALES_RETURN).
        cls.no_return_role = Role.objects.create(name="vendedor_sin_devolucion")
        for code in ("SALES_MANAGE", "INVENTORY_VIEW"):
            RolePermission.objects.create(
                role=cls.no_return_role, permission=Permission.objects.get(code=code)
            )
        cls.no_return_user = cls._create_user(
            "sindevolucion@negocio.com", cls.no_return_role
        )

        cls.warehouse = Warehouse.objects.create(name="Principal")
        for user in (cls.cashier, cls.other_cashier, cls.no_return_user):
            UserWarehouse.objects.create(user=user, warehouse=cls.warehouse)
        cls.register = CashRegister.objects.create(
            warehouse=cls.warehouse, name="Caja 1"
        )
        cls.category = Category.objects.create(name="Ropa")
        cls.customer = Customer.objects.create(
            document_type="DNI", document_number="22222222", name="Cliente"
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
        Role.objects.filter(id=self.seller_role.id).update(max_discount_percent=0)
        self.seller_role.refresh_from_db()
        self.session = CashSession.objects.create(
            cash_register=self.register,
            user=self.cashier,
            opening_amount=Decimal("0"),
            opening_at=timezone.now(),
            status="OPEN",
        )

    def tearDown(self):
        CashSession.objects.filter(status="OPEN").update(status="CLOSED")

    # --- helpers ----------------------------------------------------------

    def _client_as(self, user):
        client = APIClient(HTTP_HOST=self.domain.domain)
        login = client.post(
            "/api/v1/auth/login/",
            {"email": user.email, "password": self.password},
            format="json",
        )
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {login.data['access']}")
        return client

    _sku_counter = 0

    def _variant(self, price="100.00"):
        SupervisorAuthorizationTests._sku_counter += 1
        product = ProductVariantService.create_product(
            product_data={
                "type": "PRODUCT",
                "name": "Polo",
                "category": self.category,
                "unit_of_measure": "UND",
            },
            variants_data=[
                {
                    "sku": f"SKU-AUTH-{SupervisorAuthorizationTests._sku_counter}",
                    "price": price,
                }
            ],
        )
        variant = product.variants.first()
        StockService.adjust_stock(
            variant=variant,
            warehouse=self.warehouse,
            counted_quantity=Decimal("50"),
            concept="ADJUSTMENT",
            user=self.admin,
        )
        return variant

    def _sale(self, total="100.00"):
        variant = self._variant(price=total)
        return SaleService.create_sale(
            customer=self.customer,
            cash_session=self.session,
            user=self.cashier,
            lines=[{"variant_id": variant.id, "quantity": Decimal("1")}],
            payments=[{"method": "CASH", "amount": Decimal(total)}],
        )

    def _grant(self, client, *, supervisor=None, password=None, **payload):
        supervisor = supervisor or self.manager
        return client.post(
            "/api/v1/usuarios/authorizations/",
            {
                "email": supervisor.email,
                "password": password or self.password,
                **payload,
            },
            format="json",
        )

    def _token(self, client, **payload):
        response = self._grant(client, **payload)
        self.assertEqual(response.status_code, 201, response.data)
        return response.data["token"]

    def _error_code(self, response):
        return response.data["error"]["code"]

    def _details(self, log):
        return json.loads(log.details)

    # --- C.1: anulacion ---------------------------------------------------

    def test_cashier_without_permission_is_asked_for_authorization(self):
        sale = self._sale()
        response = self._client_as(self.cashier).post(
            f"/api/v1/ventas/sales/{sale.id}/void/", {"reason": "error"}, format="json"
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(
            self._error_code(response), "SUPERVISOR_AUTHORIZATION_REQUIRED"
        )
        self.assertEqual(response.data["error"]["permission"], "SALES_VOID")

    def test_cashier_voids_with_a_valid_authorization(self):
        sale = self._sale()
        client = self._client_as(self.cashier)
        token = self._token(client, permission="SALES_VOID", target_id=sale.id)

        response = client.post(
            f"/api/v1/ventas/sales/{sale.id}/void/",
            {"reason": "cobro duplicado"},
            format="json",
            **{_HEADER: token},
        )

        self.assertEqual(response.status_code, 200, response.data)
        sale.refresh_from_db()
        self.assertEqual(sale.status, "VOIDED")

        voided = AuditLog.objects.get(action="SALE_VOIDED", entity_id=sale.id)
        self.assertEqual(voided.user_id, self.cashier.id)
        self.assertEqual(self._details(voided)["authorized_by"], self.manager.id)

        granted = AuditLog.objects.get(action="SUPERVISOR_AUTHORIZATION_GRANTED")
        self.assertEqual(granted.user_id, self.manager.id)
        self.assertEqual(self._details(granted)["requested_by"], self.cashier.id)
        self.assertEqual(self._details(granted)["permission"], "SALES_VOID")

    def test_activity_log_shows_who_authorized(self):
        sale = self._sale()
        client = self._client_as(self.cashier)
        token = self._token(client, permission="SALES_VOID", target_id=sale.id)
        client.post(
            f"/api/v1/ventas/sales/{sale.id}/void/",
            {"reason": "error"},
            format="json",
            **{_HEADER: token},
        )

        response = self._client_as(self.admin).get(
            "/api/v1/usuarios/audit-logs/?action=SALE_VOIDED"
        )
        rows = (
            response.data["results"]
            if isinstance(response.data, dict)
            else response.data
        )
        details = json.loads(rows[0]["details"])
        self.assertEqual(rows[0]["user_email"], self.cashier.email)
        self.assertEqual(details["authorized_by_email"], self.manager.email)

    def test_token_is_single_use(self):
        first, second = self._sale(), self._sale()
        client = self._client_as(self.cashier)
        token = self._token(client, permission="SALES_VOID", target_id=first.id)
        client.post(
            f"/api/v1/ventas/sales/{first.id}/void/",
            {"reason": "error"},
            format="json",
            **{_HEADER: token},
        )

        again = client.post(
            f"/api/v1/ventas/sales/{first.id}/void/",
            {"reason": "error"},
            format="json",
            **{_HEADER: token},
        )
        self.assertEqual(again.status_code, 403)
        self.assertEqual(self._error_code(again), "SUPERVISOR_AUTHORIZATION_INVALID")
        second.refresh_from_db()
        self.assertEqual(second.status, "COMPLETED")

    def test_token_is_bound_to_its_sale(self):
        authorized, other = self._sale(), self._sale()
        client = self._client_as(self.cashier)
        token = self._token(client, permission="SALES_VOID", target_id=authorized.id)

        response = client.post(
            f"/api/v1/ventas/sales/{other.id}/void/",
            {"reason": "error"},
            format="json",
            **{_HEADER: token},
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self._error_code(response), "SUPERVISOR_AUTHORIZATION_INVALID")
        other.refresh_from_db()
        self.assertEqual(other.status, "COMPLETED")

    def test_token_is_bound_to_its_operation(self):
        sale = self._sale()
        client = self._client_as(self.no_return_user)
        token = self._token(client, permission="SALES_VOID", target_id=sale.id)

        response = client.post(
            "/api/v1/ventas/sale-returns/",
            {
                "sale_id": sale.id,
                "refund_type": "BALANCE",
                "items": [
                    {
                        "sale_detail_id": sale.details.first().id,
                        "quantity_returned": "1",
                    }
                ],
            },
            format="json",
            **{_HEADER: token},
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self._error_code(response), "SUPERVISOR_AUTHORIZATION_INVALID")

    def test_expired_token_is_rejected(self):
        sale = self._sale()
        client = self._client_as(self.cashier)
        token = self._token(client, permission="SALES_VOID", target_id=sale.id)
        SupervisorAuthorization.objects.update(
            expires_at=timezone.now() - timedelta(seconds=1)
        )

        response = client.post(
            f"/api/v1/ventas/sales/{sale.id}/void/",
            {"reason": "error"},
            format="json",
            **{_HEADER: token},
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self._error_code(response), "SUPERVISOR_AUTHORIZATION_INVALID")

    def test_token_only_works_for_the_cashier_who_asked(self):
        sale = self._sale()
        token = self._token(
            self._client_as(self.cashier), permission="SALES_VOID", target_id=sale.id
        )
        response = self._client_as(self.other_cashier).post(
            f"/api/v1/ventas/sales/{sale.id}/void/",
            {"reason": "error"},
            format="json",
            **{_HEADER: token},
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self._error_code(response), "SUPERVISOR_AUTHORIZATION_INVALID")

    def test_failed_operation_leaves_the_token_available(self):
        sale = self._sale()
        client = self._client_as(self.cashier)
        token = self._token(client, permission="SALES_VOID", target_id=sale.id)
        CashSession.objects.filter(id=self.session.id).update(status="CLOSED")

        response = client.post(
            f"/api/v1/ventas/sales/{sale.id}/void/",
            {"reason": "error"},
            format="json",
            **{_HEADER: token},
        )
        self.assertEqual(response.status_code, 409)
        self.assertIsNone(SupervisorAuthorization.objects.get().used_at)

    # --- C.1: devolucion --------------------------------------------------

    def test_return_without_permission_needs_authorization(self):
        sale = self._sale()
        client = self._client_as(self.no_return_user)
        payload = {
            "sale_id": sale.id,
            "refund_type": "BALANCE",
            "items": [
                {"sale_detail_id": sale.details.first().id, "quantity_returned": "1"}
            ],
        }

        denied = client.post("/api/v1/ventas/sale-returns/", payload, format="json")
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(denied.data["error"]["permission"], "SALES_RETURN")

        token = self._token(client, permission="SALES_RETURN", target_id=sale.id)
        response = client.post(
            "/api/v1/ventas/sale-returns/", payload, format="json", **{_HEADER: token}
        )
        self.assertEqual(response.status_code, 201, response.data)
        log = AuditLog.objects.get(action="SALE_RETURNED")
        self.assertEqual(log.user_id, self.no_return_user.id)
        self.assertEqual(self._details(log)["authorized_by"], self.manager.id)

    # --- C.1: quien puede autorizar ---------------------------------------

    def test_wrong_password_is_rejected_and_recorded(self):
        sale = self._sale()
        response = self._grant(
            self._client_as(self.cashier),
            password="otra-clave",
            permission="SALES_VOID",
            target_id=sale.id,
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self._error_code(response), "INVALID_CREDENTIALS")
        failed = AuditLog.objects.get(action="SUPERVISOR_AUTHORIZATION_FAILED")
        self.assertEqual(failed.user_id, self.cashier.id)
        self.assertEqual(failed.entity_id, self.manager.id)
        self.assertFalse(SupervisorAuthorization.objects.exists())

    def test_unknown_email_goes_to_login_attempts(self):
        response = self._client_as(self.cashier).post(
            "/api/v1/usuarios/authorizations/",
            {
                "email": "nadie@negocio.com",
                "password": "x",
                "permission": "SALES_VOID",
                "target_id": 1,
            },
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self._error_code(response), "INVALID_CREDENTIALS")
        attempt = LoginAttempt.objects.get()
        self.assertEqual(attempt.email, "nadie@negocio.com")
        self.assertEqual(attempt.source, "SUPERVISOR_AUTHORIZATION")

    def test_someone_without_the_permission_cannot_authorize(self):
        sale = self._sale()
        response = self._grant(
            self._client_as(self.cashier),
            supervisor=self.other_cashier,
            permission="SALES_VOID",
            target_id=sale.id,
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self._error_code(response), "SUPERVISOR_LACKS_PERMISSION")

    def test_nobody_authorizes_themselves(self):
        sale = self._sale()
        response = self._grant(
            self._client_as(self.cashier),
            supervisor=self.cashier,
            permission="SALES_VOID",
            target_id=sale.id,
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self._error_code(response), "SELF_AUTHORIZATION_NOT_ALLOWED")

    def test_void_and_return_authorizations_need_a_sale(self):
        response = self._grant(self._client_as(self.cashier), permission="SALES_VOID")
        self.assertEqual(response.status_code, 400)

    def test_token_is_stored_hashed(self):
        sale = self._sale()
        token = self._token(
            self._client_as(self.cashier), permission="SALES_VOID", target_id=sale.id
        )
        self.assertNotEqual(SupervisorAuthorization.objects.get().token_hash, token)

    # --- C.2: tope de descuento -------------------------------------------

    def _sale_payload(self, lines):
        total = sum(
            Decimal(line["price"]) - Decimal(line["discount"]) for line in lines
        )
        return {
            "customer_id": self.customer.id,
            "cash_session_id": self.session.id,
            "lines": [
                {
                    "variant_id": self._variant(price=line["price"]).id,
                    "quantity": "1",
                    "discount_amount": line["discount"],
                }
                for line in lines
            ],
            "payments": [{"method": "CASH", "amount": str(total)}],
        }

    def test_discount_over_the_role_limit_asks_for_authorization(self):
        payload = self._sale_payload([{"price": "100.00", "discount": "10.00"}])
        response = self._client_as(self.cashier).post(
            "/api/v1/ventas/sales/", payload, format="json"
        )
        self.assertEqual(response.status_code, 403)
        error = response.data["error"]
        self.assertEqual(error["code"], "SUPERVISOR_AUTHORIZATION_REQUIRED")
        self.assertEqual(error["permission"], "SALES_DISCOUNT")
        self.assertEqual(error["requested_discount_percent"], "10.00")
        self.assertEqual(error["max_discount_percent"], "0.00")
        self.assertFalse(Sale.objects.filter(user=self.cashier).exists())

    def test_discount_with_authorization_is_accepted_and_recorded(self):
        client = self._client_as(self.cashier)
        payload = self._sale_payload([{"price": "100.00", "discount": "10.00"}])
        token = self._token(client, permission="SALES_DISCOUNT", discount_percent="10")

        response = client.post(
            "/api/v1/ventas/sales/", payload, format="json", **{_HEADER: token}
        )
        self.assertEqual(response.status_code, 201, response.data)
        log = AuditLog.objects.get(action="SALE_CREATED", entity_id=response.data["id"])
        self.assertEqual(self._details(log)["authorized_by"], self.manager.id)
        self.assertEqual(self._details(log)["manual_discount_percent"], "10.00")

    def test_authorization_does_not_cover_a_bigger_discount(self):
        client = self._client_as(self.cashier)
        payload = self._sale_payload([{"price": "100.00", "discount": "20.00"}])
        token = self._token(client, permission="SALES_DISCOUNT", discount_percent="10")

        response = client.post(
            "/api/v1/ventas/sales/", payload, format="json", **{_HEADER: token}
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self._error_code(response), "SUPERVISOR_AUTHORIZATION_INVALID")

    def test_limit_is_per_line(self):
        Role.objects.filter(id=self.seller_role.id).update(max_discount_percent=10)
        payload = self._sale_payload(
            [
                {"price": "1000.00", "discount": "0.00"},
                {"price": "10.00", "discount": "5.00"},
            ]
        )
        response = self._client_as(self.cashier).post(
            "/api/v1/ventas/sales/", payload, format="json"
        )
        # 5 sobre 1010 es menos del 1% del total, pero 50% de esa linea.
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data["error"]["requested_discount_percent"], "50.00")

    def test_discount_within_the_role_limit_needs_nothing(self):
        Role.objects.filter(id=self.seller_role.id).update(max_discount_percent=15)
        payload = self._sale_payload([{"price": "100.00", "discount": "15.00"}])
        response = self._client_as(self.cashier).post(
            "/api/v1/ventas/sales/", payload, format="json"
        )
        self.assertEqual(response.status_code, 201, response.data)

    def test_sales_discount_permission_has_no_limit(self):
        self.session.user = self.admin
        self.session.save(update_fields=["user"])
        payload = self._sale_payload([{"price": "100.00", "discount": "90.00"}])
        response = self._client_as(self.admin).post(
            "/api/v1/ventas/sales/", payload, format="json"
        )
        self.assertEqual(response.status_code, 201, response.data)

    def test_promotion_discount_does_not_count_against_the_limit(self):
        # Sin discount_amount explicito el descuento es el de la promocion
        # vigente, que decidio el dueño: nunca pide autorizacion aunque el
        # tope del cajero sea 0.
        variant = self._variant(price="50.00")
        promotion = Promotion.objects.create(
            name="Mitad de precio",
            type="PERCENTAGE",
            value=Decimal("50"),
            start_date=timezone.now() - timedelta(days=1),
            end_date=timezone.now() + timedelta(days=1),
        )
        PromotionProduct.objects.create(promotion=promotion, variant=variant)
        response = self._client_as(self.cashier).post(
            "/api/v1/ventas/sales/",
            {
                "customer_id": self.customer.id,
                "cash_session_id": self.session.id,
                "lines": [{"variant_id": variant.id, "quantity": "1"}],
                "payments": [{"method": "CASH", "amount": "25.00"}],
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(Decimal(response.data["discount_total"]), Decimal("25"))

    def test_offline_sale_over_the_limit_is_kept_and_flagged(self):
        payload = self._sale_payload([{"price": "100.00", "discount": "30.00"}])
        response = self._client_as(self.cashier).post(
            "/api/v1/ventas/sales/sync/",
            {
                "sales": [
                    {
                        **payload,
                        "client_side_uuid": "offline-descuento-1",
                    }
                ]
            },
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["synced"][0]["status"], "CREATED")
        flag = AuditLog.objects.get(action="SALE_DISCOUNT_OVER_LIMIT")
        self.assertEqual(flag.entity_id, response.data["synced"][0]["sale_id"])
        self.assertEqual(self._details(flag)["manual_discount_percent"], "30.00")

    def test_quote_discount_is_checked_when_quoting_not_when_converting(self):
        variant = self._variant(price="100.00")
        quote_payload = {
            "customer_id": self.customer.id,
            "valid_until": (timezone.now() + timedelta(days=5)).isoformat(),
            "lines": [
                {"variant_id": variant.id, "quantity": "1", "discount_amount": "25.00"}
            ],
        }
        cashier = self._client_as(self.cashier)
        denied = cashier.post("/api/v1/ventas/quotes/", quote_payload, format="json")
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(denied.data["error"]["permission"], "SALES_DISCOUNT")

        created = self._client_as(self.manager).post(
            "/api/v1/ventas/quotes/", quote_payload, format="json"
        )
        self.assertEqual(created.status_code, 201, created.data)
        Quote.objects.filter(id=created.data["id"]).update(status="ACCEPTED")

        converted = cashier.post(
            f"/api/v1/ventas/quotes/{created.data['id']}/convert/",
            {
                "cash_session_id": self.session.id,
                "payments": [{"method": "CASH", "amount": "75.00"}],
            },
            format="json",
        )
        self.assertEqual(converted.status_code, 201, converted.data)

    def test_role_limit_is_validated(self):
        response = self._client_as(self.admin).patch(
            f"/api/v1/usuarios/roles/{self.seller_role.id}/",
            {"max_discount_percent": "150"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)


class LoginAttemptTests(TenantTestCase):
    """C.4: intentos con correos que no son de nadie del negocio."""

    @classmethod
    def get_test_schema_name(cls):
        return "test_usuarios_login_attempts"

    @classmethod
    def get_test_tenant_domain(cls):
        return "test-usuarios-login-attempts.test.com"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.password = "ClaveSegura123"
        cls.admin = User.objects.create(
            email="admin@negocio.com", role=Role.objects.get(name="admin")
        )
        cls.admin.set_password(cls.password)
        cls.admin.save()
        cls.seller = User.objects.create(
            email="cajero@negocio.com", role=Role.objects.get(name="seller")
        )
        cls.seller.set_password(cls.password)
        cls.seller.save()

    @classmethod
    def tearDownClass(cls):
        TenantSettings.objects.filter(tenant=cls.tenant).delete()
        super().tearDownClass()

    def setUp(self):
        cache.clear()

    def _client(self):
        return APIClient(HTTP_HOST=self.domain.domain)

    def _login(self, email, password):
        return self._client().post(
            "/api/v1/auth/login/",
            {"email": email, "password": password},
            format="json",
            HTTP_USER_AGENT="navegador-de-prueba",
        )

    def _client_as(self, user):
        client = self._client()
        login = client.post(
            "/api/v1/auth/login/",
            {"email": user.email, "password": self.password},
            format="json",
        )
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {login.data['access']}")
        return client

    def test_unknown_email_is_recorded(self):
        response = self._login("inventado@ataque.com", "123456")
        self.assertEqual(response.status_code, 400)
        attempt = LoginAttempt.objects.get()
        self.assertEqual(attempt.email, "inventado@ataque.com")
        self.assertEqual(attempt.source, "LOGIN")
        self.assertEqual(attempt.user_agent, "navegador-de-prueba")

    def test_known_email_stays_in_the_audit_log_only(self):
        self._login(self.seller.email, "otra-clave")
        self.assertFalse(LoginAttempt.objects.exists())
        self.assertTrue(AuditLog.objects.filter(action="LOGIN_FAILED").exists())

    def test_listing_requires_audit_permission(self):
        self._login("inventado@ataque.com", "123456")
        self.assertEqual(
            self._client_as(self.seller)
            .get("/api/v1/usuarios/login-attempts/")
            .status_code,
            403,
        )
        response = self._client_as(self.admin).get(
            "/api/v1/usuarios/login-attempts/?email=inventado"
        )
        self.assertEqual(response.status_code, 200)
        rows = (
            response.data["results"]
            if isinstance(response.data, dict)
            else response.data
        )
        self.assertEqual([row["email"] for row in rows], ["inventado@ataque.com"])

    def test_daily_cap_stops_the_table_from_growing(self):
        with mock.patch.object(LoginAttemptService, "DAILY_CAP", 2):
            for index in range(4):
                self._login(f"bot{index}@ataque.com", "123456")
        self.assertEqual(LoginAttempt.objects.count(), 2)

    def test_old_attempts_are_purged(self):
        self._login("viejo@ataque.com", "123456")
        LoginAttempt.objects.update(created_at=timezone.now() - timedelta(days=31))
        self._login("nuevo@ataque.com", "123456")

        self.assertEqual(LoginAttemptService.purge_expired(), 1)
        self.assertEqual(
            list(LoginAttempt.objects.values_list("email", flat=True)),
            ["nuevo@ataque.com"],
        )
