# Bloque B: bitacora completa del tenant -registro automatico de los CRUD
# (TenantAuditMixin), acciones fuera de un ViewSet, consulta y exportacion.
import csv
import io
import json
from datetime import datetime, timedelta

from django.core.cache import cache
from django.test import SimpleTestCase
from django.urls import get_resolver
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase
from rest_framework import mixins
from rest_framework.test import APIClient

from core.models import TenantSettings
from inventario.models import Category, Warehouse
from inventario.services import ProductVariantService, StockService
from usuarios.audit import PERSONAL_DATA_MASK, TenantAuditMixin
from usuarios.models import AuditLog, Permission, Role, RolePermission, User
from usuarios.services import AuditLogService
from ventas.models import CashRegister, Customer


class TenantAuditTests(TenantTestCase):
    @classmethod
    def get_test_schema_name(cls):
        return "test_usuarios_audit"

    @classmethod
    def get_test_tenant_domain(cls):
        return "test-usuarios-audit.test.com"

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
        # Puede leer la bitacora pero no ve costos (Bloque A.5).
        auditor_role = Role.objects.create(name="auditor")
        RolePermission.objects.create(
            role=auditor_role,
            permission=Permission.objects.get(code="USERS_VIEW_AUDIT"),
        )
        cls.auditor_user = cls._create_user("auditor@negocio.com", auditor_role)

        cls.category = Category.objects.create(name="Ropa")
        cls.warehouse = Warehouse.objects.create(name="Principal")

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

    def _logs(self, **filters):
        return AuditLog.objects.filter(**filters).order_by("id")

    # --- B.1: mixin -----------------------------------------------------

    def test_product_create_update_delete_leave_three_records(self):
        client = self._client_as(self.admin_user)
        created = client.post(
            "/api/v1/inventario/products/",
            {
                "type": "PRODUCT",
                "name": "Polo",
                "category": self.category.id,
                "unit_of_measure": "UND",
                "variants_input": [{"sku": "AUD-POLO"}],
            },
            format="json",
        )
        self.assertEqual(created.status_code, 201)
        product_id = created.data["id"]

        client.patch(
            f"/api/v1/inventario/products/{product_id}/",
            {"name": "Polo clasico"},
            format="json",
        )
        client.delete(f"/api/v1/inventario/products/{product_id}/")

        logs = list(self._logs(entity="Product", entity_id=product_id))
        self.assertEqual([log.action for log in logs], ["CREATE", "UPDATE", "DELETE"])
        self.assertTrue(all(log.user_id == self.admin_user.id for log in logs))

        self.assertEqual(json.loads(logs[0].details)["name"], "Polo")
        # La edicion guarda solo lo que cambio, con antes y despues.
        self.assertEqual(
            json.loads(logs[1].details),
            {"name": {"before": "Polo", "after": "Polo clasico"}},
        )
        self.assertEqual(json.loads(logs[2].details)["name"], "Polo clasico")

    def test_patch_without_changes_is_not_recorded(self):
        category = Category.objects.create(name="Sin cambios")
        self._client_as(self.admin_user).patch(
            f"/api/v1/inventario/categories/{category.id}/",
            {"name": "Sin cambios"},
            format="json",
        )
        self.assertFalse(self._logs(entity="Category", entity_id=category.id).exists())

    def test_sensitive_fields_never_reach_the_details(self):
        client = self._client_as(self.admin_user)
        response = client.post(
            "/api/v1/usuarios/users/",
            {
                "email": "nuevo@negocio.com",
                "password": "OtraClave123",
                "role": Role.objects.get(name="seller").id,
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        log = self._logs(entity="User", entity_id=response.data["id"]).get()
        self.assertEqual(log.action, "CREATE")
        self.assertNotIn("password", log.details)
        self.assertNotIn("OtraClave123", log.details)
        # El correo es dato personal: se registra que existe, no cual es.
        self.assertNotIn("nuevo@negocio.com", log.details)
        self.assertEqual(json.loads(log.details)["email"], PERSONAL_DATA_MASK)

        client.patch(
            f"/api/v1/usuarios/users/{response.data['id']}/",
            {"email": "cambiado@negocio.com"},
            format="json",
        )
        update = self._logs(
            entity="User", entity_id=response.data["id"], action="UPDATE"
        ).get()
        self.assertEqual(
            json.loads(update.details),
            {"email": {"before": PERSONAL_DATA_MASK, "after": PERSONAL_DATA_MASK}},
        )

    def test_role_deletion_through_service_is_recorded(self):
        role = Role.objects.create(name="temporal")
        response = self._client_as(self.admin_user).delete(
            f"/api/v1/usuarios/roles/{role.id}/"
        )
        self.assertEqual(response.status_code, 204)
        self.assertTrue(
            self._logs(entity="Role", entity_id=role.id, action="DELETE").exists()
        )

    def test_rejected_write_leaves_no_record(self):
        # Una sola variante: el backend se niega a borrarla (400), y un
        # intento fallido no es una escritura.
        product = ProductVariantService.create_product(
            product_data={
                "type": "PRODUCT",
                "name": "Unica",
                "category": self.category,
                "unit_of_measure": "UND",
            },
            variants_data=[{"sku": "AUD-UNICA"}],
        )
        variant = product.variants.first()
        response = self._client_as(self.admin_user).delete(
            f"/api/v1/inventario/product-variants/{variant.id}/"
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(
            self._logs(entity="ProductVariant", entity_id=variant.id).exists()
        )

    def test_sale_keeps_a_single_record(self):
        product = ProductVariantService.create_product(
            product_data={
                "type": "PRODUCT",
                "name": "Gaseosa",
                "category": self.category,
                "unit_of_measure": "UND",
            },
            variants_data=[{"sku": "AUD-SALE", "price": "5.00"}],
        )
        variant = product.variants.first()
        StockService.adjust_stock(
            variant=variant,
            warehouse=self.warehouse,
            counted_quantity=10,
            concept="ADJUSTMENT",
            user=self.admin_user,
        )
        customer = Customer.objects.create(
            document_type="DNI", document_number="12312312", name="Cliente"
        )
        register = CashRegister.objects.create(warehouse=self.warehouse, name="Caja A")
        client = self._client_as(self.admin_user)
        session = client.post(
            "/api/v1/ventas/cash-sessions/open/",
            {"cash_register_id": register.id, "opening_amount": "0"},
            format="json",
        )
        self.assertTrue(
            self._logs(
                action="CASH_SESSION_OPENED", entity_id=session.data["id"]
            ).exists()
        )

        before = AuditLog.objects.count()
        response = client.post(
            "/api/v1/ventas/sales/",
            {
                "customer_id": customer.id,
                "cash_session_id": session.data["id"],
                "lines": [{"variant_id": variant.id, "quantity": "2"}],
                "payments": [{"method": "CASH", "amount": "10.00"}],
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        new_logs = list(AuditLog.objects.order_by("id")[before:])
        self.assertEqual([log.action for log in new_logs], ["SALE_CREATED"])

    # --- B.2: acciones fuera de un ViewSet -------------------------------

    def test_login_failed_login_and_logout_are_recorded(self):
        client = APIClient(HTTP_HOST=self.domain.domain)
        client.post(
            "/api/v1/auth/login/",
            {"email": self.seller_user.email, "password": "incorrecta"},
            format="json",
        )
        client.post(
            "/api/v1/auth/login/",
            {"email": self.seller_user.email, "password": self.password},
            format="json",
        )
        client.post("/api/v1/auth/logout/")

        actions = list(
            self._logs(user=self.seller_user, entity="User").values_list(
                "action", flat=True
            )
        )
        self.assertEqual(actions, ["LOGIN_FAILED", "LOGIN", "LOGOUT"])
        login = self._logs(user=self.seller_user, action="LOGIN").get()
        self.assertIn("ip", json.loads(login.details))

    # --- B.3: consulta y exportacion ------------------------------------

    def test_user_without_audit_permission_gets_403(self):
        response = self._client_as(self.seller_user).get("/api/v1/usuarios/audit-logs/")
        self.assertEqual(response.status_code, 403)

    def _seed_filter_logs(self):
        AuditLog.objects.all().delete()
        AuditLogService.log_action(
            user=self.admin_user, action="CREATE", entity="Category", entity_id=1
        )
        AuditLogService.log_action(
            user=self.admin_user, action="UPDATE", entity="Category", entity_id=1
        )
        AuditLogService.log_action(
            user=self.seller_user, action="CREATE", entity="Customer", entity_id=7
        )
        old = AuditLogService.log_action(
            user=self.seller_user, action="CREATE", entity="Customer", entity_id=8
        )
        AuditLog.objects.filter(pk=old.pk).update(
            created_at=timezone.now() - timedelta(days=10)
        )

    def _results(self, response):
        data = response.data
        return data["results"] if isinstance(data, dict) else data

    def test_filters_narrow_the_listing(self):
        self._seed_filter_logs()
        client = self._client_as(self.admin_user)
        # El propio login del admin tambien queda en la bitacora; se filtra
        # por entidad para no depender de el.
        base = "/api/v1/usuarios/audit-logs/"

        by_entity = self._results(client.get(base, {"entity": "Category"}))
        self.assertEqual(len(by_entity), 2)

        by_action = self._results(
            client.get(base, {"entity": "Category", "action": "UPDATE"})
        )
        self.assertEqual(len(by_action), 1)

        by_user = self._results(
            client.get(base, {"user": self.seller_user.id, "entity": "Customer"})
        )
        self.assertEqual(len(by_user), 2)
        self.assertEqual(by_user[0]["user_email"], self.seller_user.email)

        by_id = self._results(client.get(base, {"entity_id": 7}))
        self.assertEqual([row["entity"] for row in by_id], ["Customer"])

        today = timezone.localdate().isoformat()
        recent = self._results(
            client.get(base, {"entity": "Customer", "date_from": today})
        )
        self.assertEqual([row["entity_id"] for row in recent], [7])

        self.assertEqual(client.get(base, {"user": "abc"}).status_code, 400)

    def test_export_respects_the_filter_and_is_recorded(self):
        self._seed_filter_logs()
        response = self._client_as(self.admin_user).get(
            "/api/v1/usuarios/audit-logs/",
            {
                "entity": "Category",
                "export": "csv",
                "date_from": timezone.localdate().isoformat(),
                "date_to": timezone.localdate().isoformat(),
            },
        )
        self.assertEqual(response.status_code, 200)
        rows = list(csv.DictReader(io.StringIO(response.content.decode())))
        self.assertEqual(len(rows), 2)
        self.assertEqual({row["entity"] for row in rows}, {"Category"})
        self.assertEqual(rows[0]["user_email"], self.admin_user.email)

        exported = self._logs(action="AUDIT_LOG_EXPORTED").get()
        self.assertEqual(json.loads(exported.details)["rows"], 2)

    def test_export_rejects_unknown_format(self):
        response = self._client_as(self.admin_user).get(
            "/api/v1/usuarios/audit-logs/", {"export": "pdf"}
        )
        self.assertEqual(response.status_code, 400)

    def test_export_requires_a_bounded_date_range(self):
        client = self._client_as(self.admin_user)
        base = "/api/v1/usuarios/audit-logs/"
        # Sin fechas: bajaria toda la historia de una vez.
        self.assertEqual(client.get(base, {"export": "csv"}).status_code, 400)
        # Mas de 366 dias.
        too_long = client.get(
            base,
            {"export": "csv", "date_from": "2025-01-01", "date_to": "2026-01-02"},
        )
        self.assertEqual(too_long.status_code, 400)
        # Un año completo, justo en el limite.
        one_year = client.get(
            base,
            {"export": "csv", "date_from": "2025-01-01", "date_to": "2026-01-01"},
        )
        self.assertEqual(one_year.status_code, 200)
        self.assertFalse(self._logs(action="AUDIT_LOG_EXPORTED").count() > 1)

    def test_invalid_or_reversed_dates_answer_400_not_500(self):
        client = self._client_as(self.admin_user)
        base = "/api/v1/usuarios/audit-logs/"
        self.assertEqual(client.get(base, {"date_from": "hoy"}).status_code, 400)
        reversed_range = client.get(
            base,
            {"export": "csv", "date_from": "2026-02-01", "date_to": "2026-01-01"},
        )
        self.assertEqual(reversed_range.status_code, 400)
        # Mismo helper en los reportes que ya existian.
        report = client.get(
            "/api/v1/ventas/reports/sales/",
            {"date_from": "2026-13-01", "date_to": "2026-01-01"},
        )
        self.assertEqual(report.status_code, 400)

    def test_date_filter_uses_the_business_day_in_lima(self):
        AuditLog.objects.all().delete()
        late = AuditLogService.log_action(
            user=self.admin_user, action="CREATE", entity="Brand", entity_id=1
        )
        # 23:30 del 10 de marzo en Lima son las 04:30 del 11 en UTC: sigue
        # siendo el 10 para el negocio.
        lima_night = timezone.make_aware(datetime(2026, 3, 10, 23, 30))
        AuditLog.objects.filter(pk=late.pk).update(created_at=lima_night)

        def ids(**params):
            rows = self._results(
                self._client_as(self.admin_user).get(
                    "/api/v1/usuarios/audit-logs/", {"entity": "Brand", **params}
                )
            )
            return [row["id"] for row in rows]

        self.assertEqual(ids(date_from="2026-03-10", date_to="2026-03-10"), [late.pk])
        self.assertEqual(ids(date_from="2026-03-11"), [])

    def test_cost_is_hidden_from_a_viewer_without_cost_permission(self):
        product = ProductVariantService.create_product(
            product_data={
                "type": "PRODUCT",
                "name": "Con costo",
                "category": self.category,
                "unit_of_measure": "UND",
            },
            variants_data=[{"sku": "AUD-COST", "cost": "3.00"}],
        )
        variant = product.variants.first()
        self._client_as(self.admin_user).patch(
            f"/api/v1/inventario/product-variants/{variant.id}/",
            {"cost": "4.50"},
            format="json",
        )

        def cost_change(user):
            rows = self._results(
                self._client_as(user).get(
                    "/api/v1/usuarios/audit-logs/",
                    {"entity": "ProductVariant", "entity_id": variant.id},
                )
            )
            return json.loads(rows[0]["details"])

        self.assertEqual(
            cost_change(self.admin_user)["cost"],
            {"before": "3.0000", "after": "4.5000"},
        )
        self.assertNotIn("cost", cost_change(self.auditor_user))


class AuditCoverageTests(SimpleTestCase):
    """Guarda del criterio "toda escritura queda registrada": un ViewSet
    nuevo de las apps de negocio con escritura generica tiene que pasar por
    TenantAuditMixin o figurar aqui con el motivo."""

    # create() propio que delega en un servicio que ya registra con mas
    # contexto que un CREATE generico (o, en gimnasio, la vista registra).
    _LOGGED_BY_SERVICE = {
        "SaleViewSet",
        "SaleReturnViewSet",
        "ProductReservationViewSet",
        "QuoteViewSet",
        "MembershipViewSet",
        "ClassBookingViewSet",
        "MembershipGroupViewSet",
    }

    def _viewsets(self, patterns):
        for pattern in patterns:
            if hasattr(pattern, "url_patterns"):
                yield from self._viewsets(pattern.url_patterns)
                continue
            cls = getattr(pattern.callback, "cls", None)
            if cls is not None and cls.__module__.split(".")[0] in (
                "inventario",
                "ventas",
                "usuarios",
                "gimnasio",
            ):
                yield cls

    def test_every_generic_write_viewset_is_audited(self):
        write_mixins = (
            mixins.CreateModelMixin,
            mixins.UpdateModelMixin,
            mixins.DestroyModelMixin,
        )
        unaudited = sorted(
            {
                cls.__name__
                for cls in self._viewsets(get_resolver().url_patterns)
                if issubclass(cls, write_mixins)
                and not issubclass(cls, TenantAuditMixin)
                and cls.__name__ not in self._LOGGED_BY_SERVICE
            }
        )
        self.assertEqual(unaudited, [])
