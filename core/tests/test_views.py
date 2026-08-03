# Pruebas de ViewSets/vistas: permisos, serialización, códigos de respuesta HTTP.
from datetime import timedelta

from django.utils import timezone
from rest_framework.test import APIClient, APIRequestFactory, APITestCase

from core.models import Domain, PlatformStaff, Tenant, TenantSettings
from core.permissions import IsPlatformStaff, TenantNotSuspended, TenantSuspendedError


class PlatformStaffAuthTests(APITestCase):
    """Flujo JWT de platform_staff: login, refresh, logout (Sprint 1, tarea 2)."""

    @classmethod
    def setUpTestData(cls):
        # TenantMainMiddleware resuelve el schema por Host; el tenant/dominio
        # "public" debe existir para que cualquier request llegue a las vistas.
        public_tenant = Tenant.objects.create(
            schema_name="public", company_name="Servicio Publico"
        )
        Domain.objects.create(
            domain="public.localhost", tenant=public_tenant, is_primary=True
        )
        cls.password = "ClaveSegura123"
        cls.staff = PlatformStaff.objects.create(
            email="admin@fivuza.com", full_name="Admin Fivuza", role="SUPER_ADMIN"
        )
        cls.staff.set_password(cls.password)
        cls.staff.save()

    def setUp(self):
        self.client = APIClient(HTTP_HOST="public.localhost")

    def _login(self):
        return self.client.post(
            "/api/v1/platform/auth/login/",
            {"email": self.staff.email, "password": self.password},
            format="json",
        )

    def test_login_with_valid_credentials_returns_tokens(self):
        response = self._login()
        self.assertEqual(response.status_code, 200)
        self.assertIn("access", response.data)
        self.assertIn("refresh", response.data)

    def test_login_with_wrong_password_fails(self):
        response = self.client.post(
            "/api/v1/platform/auth/login/",
            {"email": self.staff.email, "password": "incorrecta"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)

    def test_login_with_inactive_staff_fails(self):
        self.staff.is_active = False
        self.staff.save()
        response = self._login()
        self.assertEqual(response.status_code, 400)

    def test_logout_requires_authentication(self):
        response = self.client.post(
            "/api/v1/platform/auth/logout/", {"refresh": "x"}, format="json"
        )
        self.assertEqual(response.status_code, 401)

    def test_refresh_then_logout_blacklists_refresh_token(self):
        tokens = self._login().data
        access, refresh = tokens["access"], tokens["refresh"]

        refresh_response = self.client.post(
            "/api/v1/platform/auth/refresh/", {"refresh": refresh}, format="json"
        )
        self.assertEqual(refresh_response.status_code, 200)

        old_refresh_reuse = self.client.post(
            "/api/v1/platform/auth/refresh/", {"refresh": refresh}, format="json"
        )
        self.assertEqual(old_refresh_reuse.status_code, 401)

        new_refresh = refresh_response.data["refresh"]
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
        logout_response = self.client.post(
            "/api/v1/platform/auth/logout/", {"refresh": new_refresh}, format="json"
        )
        self.assertEqual(logout_response.status_code, 205)

        reuse_after_logout = self.client.post(
            "/api/v1/platform/auth/refresh/", {"refresh": new_refresh}, format="json"
        )
        self.assertEqual(reuse_after_logout.status_code, 401)


class TenantLifecycleViewTests(APITestCase):
    """Suspension/reactivacion de tenants: solo platform_staff (Sprint 1, tarea 4)."""

    @classmethod
    def setUpTestData(cls):
        public_tenant = Tenant.objects.create(
            schema_name="public", company_name="Servicio Publico"
        )
        Domain.objects.create(
            domain="public.localhost", tenant=public_tenant, is_primary=True
        )
        cls.password = "ClaveSegura123"
        cls.staff = PlatformStaff.objects.create(
            email="admin@fivuza.com", full_name="Admin Fivuza", role="SUPER_ADMIN"
        )
        cls.staff.set_password(cls.password)
        cls.staff.save()
        cls.target_tenant = Tenant.objects.create(
            schema_name="test_lifecycle", company_name="Negocio Moroso"
        )

    def setUp(self):
        self.client = APIClient(HTTP_HOST="public.localhost")
        login = self.client.post(
            "/api/v1/platform/auth/login/",
            {"email": self.staff.email, "password": self.password},
            format="json",
        )
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {login.data['access']}")

    def test_suspend_sets_status_and_timestamp(self):
        response = self.client.patch(
            f"/api/v1/core/tenants/{self.target_tenant.id}/suspend/",
            {"reason": "Suscripcion vencida hace 15 dias"},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], "suspended")
        self.assertIsNotNone(response.data["suspended_at"])

    def test_reactivate_clears_suspension(self):
        self.client.patch(
            f"/api/v1/core/tenants/{self.target_tenant.id}/suspend/",
            {"reason": "x"},
            format="json",
        )
        response = self.client.patch(
            f"/api/v1/core/tenants/{self.target_tenant.id}/reactivate/",
            {},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], "active")

    def test_unauthenticated_cannot_suspend(self):
        self.client.credentials()  # limpia el token de platform_staff del setUp
        response = self.client.patch(
            f"/api/v1/core/tenants/{self.target_tenant.id}/suspend/", {}, format="json"
        )
        self.assertEqual(response.status_code, 401)

    def test_cancel_sets_status_and_timestamps(self):
        response = self.client.patch(
            f"/api/v1/core/tenants/{self.target_tenant.id}/cancel/",
            {"reason": "El negocio cerro sus operaciones"},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], "canceled")
        self.assertIsNotNone(response.data["canceled_at"])
        self.assertIsNotNone(response.data["data_retention_until"])

    def test_cancel_twice_is_rejected(self):
        self.client.patch(
            f"/api/v1/core/tenants/{self.target_tenant.id}/cancel/", {}, format="json"
        )
        response = self.client.patch(
            f"/api/v1/core/tenants/{self.target_tenant.id}/cancel/", {}, format="json"
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["error"]["code"], "TENANT_ALREADY_CANCELED")

    def test_canceled_tenant_cannot_be_reactivated(self):
        self.client.patch(
            f"/api/v1/core/tenants/{self.target_tenant.id}/cancel/", {}, format="json"
        )
        response = self.client.patch(
            f"/api/v1/core/tenants/{self.target_tenant.id}/reactivate/",
            {},
            format="json",
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.data["error"]["code"], "CANNOT_REACTIVATE_CANCELED_TENANT"
        )


class TenantNotSuspendedPermissionTests(APITestCase):
    """Permiso compartido que las 4 apps de negocio usaran para responder 402."""

    def _request_with_tenant_status(self, status):
        tenant = Tenant(schema_name="fake", company_name="Fake", status=status)
        request = APIRequestFactory().get("/")
        request.tenant = tenant
        return request

    def test_active_tenant_is_allowed(self):
        request = self._request_with_tenant_status("active")
        self.assertTrue(TenantNotSuspended().has_permission(request, None))

    def test_suspended_tenant_raises_402(self):
        request = self._request_with_tenant_status("suspended")
        with self.assertRaises(TenantSuspendedError) as ctx:
            TenantNotSuspended().has_permission(request, None)
        self.assertEqual(ctx.exception.status_code, 402)


class IsPlatformStaffPermissionTests(APITestCase):
    def test_platform_staff_instance_is_allowed(self):
        staff = PlatformStaff(email="x@fivuza.com", full_name="X", role="SUPPORT")
        request = APIRequestFactory().get("/")
        request.user = staff
        self.assertTrue(IsPlatformStaff().has_permission(request, None))

    def test_non_platform_staff_is_rejected(self):
        request = APIRequestFactory().get("/")
        request.user = object()
        self.assertFalse(IsPlatformStaff().has_permission(request, None))


class CoreCRUDEndpointsTests(APITestCase):
    """CRUD de core: tenants, plans, plan-features, subscriptions,
    subscription-payments, tenant-settings, platform-staff (Sprint 1, tarea 5)."""

    @classmethod
    def setUpTestData(cls):
        public_tenant = Tenant.objects.create(
            schema_name="public", company_name="Servicio Publico"
        )
        Domain.objects.create(
            domain="public.localhost", tenant=public_tenant, is_primary=True
        )
        cls.password = "ClaveSegura123"
        cls.super_admin = PlatformStaff.objects.create(
            email="admin@fivuza.com", full_name="Super Admin", role="SUPER_ADMIN"
        )
        cls.super_admin.set_password(cls.password)
        cls.super_admin.save()
        cls.support = PlatformStaff.objects.create(
            email="soporte@fivuza.com", full_name="Soporte", role="SUPPORT"
        )
        cls.support.set_password(cls.password)
        cls.support.save()
        cls.billing = PlatformStaff.objects.create(
            email="facturacion@fivuza.com", full_name="Billing", role="BILLING"
        )
        cls.billing.set_password(cls.password)
        cls.billing.save()

        from core.models import Plan

        cls.plan = Plan.objects.create(
            code="PLAN_1",
            name="Plan 1",
            max_users=1,
            price_monthly=29,
            price_semiannual=145,
            price_annual=290,
        )

    def _client_as(self, staff):
        client = APIClient(HTTP_HOST="public.localhost")
        login = client.post(
            "/api/v1/platform/auth/login/",
            {"email": staff.email, "password": self.password},
            format="json",
        )
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {login.data['access']}")
        return client

    def test_plans_list_is_public(self):
        response = APIClient(HTTP_HOST="public.localhost").get("/api/v1/core/plans/")
        self.assertEqual(response.status_code, 200)

    def test_plans_write_requires_super_admin(self):
        response = self._client_as(self.support).post(
            "/api/v1/core/plans/",
            {
                "code": "PLAN_2",
                "name": "Plan 2",
                "max_users": 1,
                "price_monthly": 39,
                "price_semiannual": 195,
                "price_annual": 390,
            },
            format="json",
        )
        self.assertEqual(response.status_code, 403)

        response = self._client_as(self.super_admin).post(
            "/api/v1/core/plans/",
            {
                "code": "PLAN_2",
                "name": "Plan 2",
                "max_users": 1,
                "price_monthly": 39,
                "price_semiannual": 195,
                "price_annual": 390,
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201)

    def test_tenants_list_has_no_create_action(self):
        response = self._client_as(self.super_admin).post(
            "/api/v1/core/tenants/",
            {"schema_name": "no_deberia_crearse", "company_name": "X"},
            format="json",
        )
        self.assertEqual(response.status_code, 405)

    def test_tenants_list_requires_platform_staff(self):
        response = APIClient(HTTP_HOST="public.localhost").get("/api/v1/core/tenants/")
        self.assertEqual(response.status_code, 401)

        response = self._client_as(self.support).get("/api/v1/core/tenants/")
        self.assertEqual(response.status_code, 200)

    def test_platform_staff_crud_restricted_to_super_admin(self):
        response = self._client_as(self.support).get("/api/v1/core/platform-staff/")
        self.assertEqual(response.status_code, 403)

        response = self._client_as(self.super_admin).get("/api/v1/core/platform-staff/")
        self.assertEqual(response.status_code, 200)

    def test_platform_staff_create_hashes_password(self):
        response = self._client_as(self.super_admin).post(
            "/api/v1/core/platform-staff/",
            {
                "email": "nuevo@fivuza.com",
                "full_name": "Nuevo",
                "role": "SUPPORT",
                "password": "OtraClave456",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        self.assertNotIn("password", response.data)

        created = PlatformStaff.objects.get(email="nuevo@fivuza.com")
        self.assertNotEqual(created.password, "OtraClave456")
        self.assertTrue(created.check_password("OtraClave456"))

    def test_subscription_payments_write_requires_billing_role(self):
        from core.models import Subscription

        subscription = Subscription.objects.create(
            tenant=Tenant.objects.first(),
            plan=self.plan,
            billing_cycle="MONTHLY",
            price_paid=29,
            status="active",
            starts_at="2026-01-01T00:00:00Z",
            expires_at="2026-02-01T00:00:00Z",
        )
        payload = {
            "subscription": subscription.id,
            "amount": 29,
            "payment_method": "TRANSFER",
            "status": "PAID",
        }

        response = self._client_as(self.super_admin).post(
            "/api/v1/core/subscription-payments/", payload, format="json"
        )
        self.assertEqual(response.status_code, 403)

        response = self._client_as(self.billing).post(
            "/api/v1/core/subscription-payments/", payload, format="json"
        )
        self.assertEqual(response.status_code, 201)

        # lectura: cualquier platform_staff, no solo BILLING
        response = self._client_as(self.support).get(
            "/api/v1/core/subscription-payments/"
        )
        self.assertEqual(response.status_code, 200)

    def test_plan_features_readable_by_any_staff_writable_only_by_super_admin(self):
        from core.models import PlanFeature

        # Sprint 9: antes este recurso exigia SUPER_ADMIN incluso para leer.
        response = self._client_as(self.support).get("/api/v1/core/plan-features/")
        self.assertEqual(response.status_code, 200)
        response = self._client_as(self.billing).get("/api/v1/core/plan-features/")
        self.assertEqual(response.status_code, 200)

        payload = {"plan": self.plan.id, "feature_code": "HAS_SALES_MODULE"}
        response = self._client_as(self.support).post(
            "/api/v1/core/plan-features/", payload, format="json"
        )
        self.assertEqual(response.status_code, 403)

        response = self._client_as(self.super_admin).post(
            "/api/v1/core/plan-features/", payload, format="json"
        )
        self.assertEqual(response.status_code, 201)
        self.assertTrue(PlanFeature.objects.filter(id=response.data["id"]).exists())

    def test_platform_staff_cannot_be_hard_deleted(self):
        # Sprint 9: platform_audit_logs.platform_staff es PROTECT -se retira
        # el borrado del CRUD (API Spec §2.5: "desactivacion, no borrado
        # fisico") en vez de dejar que un DELETE revienta con ProtectedError.
        response = self._client_as(self.super_admin).delete(
            f"/api/v1/core/platform-staff/{self.support.id}/"
        )
        self.assertEqual(response.status_code, 405)

    def test_platform_staff_deactivated_via_patch(self):
        response = self._client_as(self.super_admin).patch(
            f"/api/v1/core/platform-staff/{self.support.id}/",
            {"is_active": False},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.support.refresh_from_db()
        self.assertFalse(self.support.is_active)

    def test_login_response_includes_staff_role(self):
        response = APIClient(HTTP_HOST="public.localhost").post(
            "/api/v1/platform/auth/login/",
            {"email": self.billing.email, "password": self.password},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["staff"]["role"], "BILLING")
        self.assertEqual(response.data["staff"]["email"], self.billing.email)

    def test_subscriptions_write_requires_any_platform_staff(self):
        response = APIClient(HTTP_HOST="public.localhost").get(
            "/api/v1/core/subscriptions/"
        )
        self.assertEqual(response.status_code, 401)

        response = self._client_as(self.support).get("/api/v1/core/subscriptions/")
        self.assertEqual(response.status_code, 200)

    def test_tenant_settings_requires_any_platform_staff(self):
        response = self._client_as(self.billing).get("/api/v1/core/tenant-settings/")
        self.assertEqual(response.status_code, 200)

    def test_audit_logs_and_dashboard_readable_by_any_staff(self):
        for staff in (self.super_admin, self.support, self.billing):
            response = self._client_as(staff).get("/api/v1/core/platform-audit-logs/")
            self.assertEqual(response.status_code, 200)
            response = self._client_as(staff).get("/api/v1/core/dashboard/summary/")
            self.assertEqual(response.status_code, 200)


class TenantRegisterViewTests(APITestCase):
    """POST /api/v1/core/tenants/register/ (Sprint 1, cierre del gap de la
    Definicion de Hecho: "se puede crear un tenant nuevo via API")."""

    @classmethod
    def setUpTestData(cls):
        public_tenant = Tenant.objects.create(
            schema_name="public", company_name="Servicio Publico"
        )
        Domain.objects.create(
            domain="public.localhost", tenant=public_tenant, is_primary=True
        )
        cls.password = "ClaveSegura123"
        cls.super_admin = PlatformStaff.objects.create(
            email="admin@fivuza.com", full_name="Super Admin", role="SUPER_ADMIN"
        )
        cls.super_admin.set_password(cls.password)
        cls.super_admin.save()

        from core.models import Plan

        cls.plan = Plan.objects.create(
            code="PLAN_2",
            name="Plan 2",
            max_users=1,
            price_monthly=39,
            price_semiannual=195,
            price_annual=390,
        )

    def _client_as(self, staff):
        client = APIClient(HTTP_HOST="public.localhost")
        login = client.post(
            "/api/v1/platform/auth/login/",
            {"email": staff.email, "password": self.password},
            format="json",
        )
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {login.data['access']}")
        return client

    def _payload(self, **overrides):
        payload = {
            "company_name": "Bodega Lucho",
            "ruc": "20123456789",
            "schema_name": "emp_lucho",
            "domain": "lucho.fivuza.localhost",
            "plan_code": "PLAN_2",
            "billing_cycle": "MONTHLY",
        }
        payload.update(overrides)
        return payload

    def test_register_creates_tenant_domain_and_subscription(self):
        from core.models import Domain as DomainModel
        from core.models import Subscription

        response = self._client_as(self.super_admin).post(
            "/api/v1/core/tenants/register/", self._payload(), format="json"
        )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.data["status"], "trial")
        # CELERY_TASK_ALWAYS_EAGER=True en tests: provision_tenant_async ya
        # termino (sincrono, en el mismo proceso) para cuando la vista arma
        # la respuesta -en produccion, sin un worker que la tome todavia,
        # este mismo campo llegaria como "PENDING".
        self.assertEqual(response.data["provisioning_status"], "COMPLETED")

        tenant = Tenant.objects.get(schema_name="emp_lucho")
        self.assertTrue(
            DomainModel.objects.filter(
                domain="lucho.fivuza.localhost", tenant=tenant
            ).exists()
        )
        subscription = Subscription.objects.get(tenant=tenant)
        self.assertEqual(subscription.plan, self.plan)
        self.assertEqual(subscription.price_paid, 39)
        # TenantProvisioningService (signal post_save) ya debio correr:
        self.assertTrue(TenantSettings.objects.filter(tenant=tenant).exists())

    def test_register_without_ruc_succeeds(self):
        payload = self._payload(
            schema_name="emp_sin_ruc", domain="sin-ruc.fivuza.localhost"
        )
        del payload["ruc"]

        response = self._client_as(self.super_admin).post(
            "/api/v1/core/tenants/register/", payload, format="json"
        )
        self.assertEqual(response.status_code, 202)
        tenant = Tenant.objects.get(schema_name="emp_sin_ruc")
        self.assertIsNone(tenant.ruc)

    def test_register_rejects_duplicate_schema_name(self):
        client = self._client_as(self.super_admin)
        client.post("/api/v1/core/tenants/register/", self._payload(), format="json")

        response = client.post(
            "/api/v1/core/tenants/register/",
            self._payload(domain="otro-dominio.localhost"),
            format="json",
        )
        self.assertEqual(response.status_code, 400)

    def test_register_rejects_unknown_plan_code(self):
        response = self._client_as(self.super_admin).post(
            "/api/v1/core/tenants/register/",
            self._payload(plan_code="PLAN_INEXISTENTE"),
            format="json",
        )
        self.assertEqual(response.status_code, 400)

    def test_register_requires_platform_staff(self):
        response = APIClient(HTTP_HOST="public.localhost").post(
            "/api/v1/core/tenants/register/", self._payload(), format="json"
        )
        self.assertEqual(response.status_code, 401)


class PlatformAuditLogTests(APITestCase):
    """platform_audit_logs: se escribe via PlatformAuditLogService al suspender/
    reactivar/registrar un tenant, y es de solo lectura por API."""

    @classmethod
    def setUpTestData(cls):
        public_tenant = Tenant.objects.create(
            schema_name="public", company_name="Servicio Publico"
        )
        Domain.objects.create(
            domain="public.localhost", tenant=public_tenant, is_primary=True
        )
        cls.password = "ClaveSegura123"
        cls.super_admin = PlatformStaff.objects.create(
            email="admin@fivuza.com", full_name="Super Admin", role="SUPER_ADMIN"
        )
        cls.super_admin.set_password(cls.password)
        cls.super_admin.save()
        cls.target_tenant = Tenant.objects.create(
            schema_name="test_audit", company_name="Negocio Auditado"
        )

    def _client_as(self, staff):
        client = APIClient(HTTP_HOST="public.localhost")
        login = client.post(
            "/api/v1/platform/auth/login/",
            {"email": staff.email, "password": self.password},
            format="json",
        )
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {login.data['access']}")
        return client

    def test_suspend_tenant_writes_audit_log(self):
        from core.models import PlatformAuditLog

        client = self._client_as(self.super_admin)
        client.patch(
            f"/api/v1/core/tenants/{self.target_tenant.id}/suspend/",
            {"reason": "Suscripcion vencida"},
            format="json",
        )
        log = PlatformAuditLog.objects.get(
            action="SUSPEND_TENANT", entity_id=self.target_tenant.id
        )
        self.assertEqual(log.platform_staff, self.super_admin)
        self.assertEqual(log.entity, "Tenant")

    def test_plan_create_writes_audit_log(self):
        from core.models import PlatformAuditLog

        client = self._client_as(self.super_admin)
        response = client.post(
            "/api/v1/core/plans/",
            {
                "code": "PLAN_AUDIT",
                "name": "Plan Auditado",
                "max_users": 1,
                "price_monthly": 19,
                "price_semiannual": 95,
                "price_annual": 190,
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        self.assertTrue(
            PlatformAuditLog.objects.filter(
                action="CREATE", entity="Plan", entity_id=response.data["id"]
            ).exists()
        )

    def test_audit_log_list_requires_platform_staff(self):
        response = APIClient(HTTP_HOST="public.localhost").get(
            "/api/v1/core/platform-audit-logs/"
        )
        self.assertEqual(response.status_code, 401)

        response = self._client_as(self.super_admin).get(
            "/api/v1/core/platform-audit-logs/"
        )
        self.assertEqual(response.status_code, 200)

    def test_audit_log_is_read_only(self):
        response = self._client_as(self.super_admin).post(
            "/api/v1/core/platform-audit-logs/",
            {
                "platform_staff": self.super_admin.id,
                "action": "FAKE",
                "entity": "Tenant",
                "entity_id": 1,
            },
            format="json",
        )
        self.assertEqual(response.status_code, 405)


class PlatformAuditLogFilteringTests(APITestCase):
    """Paginacion, filtros y ordenamiento sobre GET /core/platform-audit-logs/
    (Sprint 8, Especificacion de API §4.14)."""

    @classmethod
    def setUpTestData(cls):
        from core.models import PlatformAuditLog

        public_tenant = Tenant.objects.create(
            schema_name="public", company_name="Servicio Publico"
        )
        Domain.objects.create(
            domain="public.localhost", tenant=public_tenant, is_primary=True
        )
        cls.password = "ClaveSegura123"
        cls.staff_a = PlatformStaff.objects.create(
            email="a@fivuza.com", full_name="Staff A", role="SUPER_ADMIN"
        )
        cls.staff_a.set_password(cls.password)
        cls.staff_a.save()
        cls.staff_b = PlatformStaff.objects.create(
            email="b@fivuza.com", full_name="Staff B", role="SUPPORT"
        )
        cls.staff_b.set_password(cls.password)
        cls.staff_b.save()

        PlatformAuditLog.objects.create(
            platform_staff=cls.staff_a,
            action="SUSPEND_TENANT",
            entity="Tenant",
            entity_id=1,
        )
        PlatformAuditLog.objects.create(
            platform_staff=cls.staff_b,
            action="REACTIVATE_TENANT",
            entity="Tenant",
            entity_id=1,
        )
        PlatformAuditLog.objects.create(
            platform_staff=cls.staff_a, action="CREATE", entity="Plan", entity_id=5
        )

    def _client_as(self, staff):
        client = APIClient(HTTP_HOST="public.localhost")
        login = client.post(
            "/api/v1/platform/auth/login/",
            {"email": staff.email, "password": self.password},
            format="json",
        )
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {login.data['access']}")
        return client

    def test_list_is_paginated(self):
        response = self._client_as(self.staff_a).get(
            "/api/v1/core/platform-audit-logs/"
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("results", response.data)
        self.assertEqual(response.data["count"], 3)

    def test_filter_by_platform_staff(self):
        response = self._client_as(self.staff_a).get(
            f"/api/v1/core/platform-audit-logs/?platform_staff={self.staff_b.id}"
        )
        self.assertEqual(len(response.data["results"]), 1)
        self.assertEqual(response.data["results"][0]["action"], "REACTIVATE_TENANT")

    def test_filter_by_entity(self):
        response = self._client_as(self.staff_a).get(
            "/api/v1/core/platform-audit-logs/?entity=Plan"
        )
        self.assertEqual(len(response.data["results"]), 1)
        self.assertEqual(response.data["results"][0]["entity"], "Plan")

    def test_filter_by_entity_and_entity_id(self):
        response = self._client_as(self.staff_a).get(
            "/api/v1/core/platform-audit-logs/?entity=Tenant&entity_id=1"
        )
        self.assertEqual(len(response.data["results"]), 2)

    def test_ordering_ascending_by_created_at(self):
        response = self._client_as(self.staff_a).get(
            "/api/v1/core/platform-audit-logs/?ordering=created_at"
        )
        actions = [row["action"] for row in response.data["results"]]
        self.assertEqual(actions, ["SUSPEND_TENANT", "REACTIVATE_TENANT", "CREATE"])


class SubscriptionPaymentConfirmViewTests(APITestCase):
    """POST /core/subscription-payments/{id}/confirm/ (Especificacion de API
    §4.10). Solo rol BILLING."""

    @classmethod
    def setUpTestData(cls):
        from core.models import Plan

        public_tenant = Tenant.objects.create(
            schema_name="public", company_name="Servicio Publico"
        )
        Domain.objects.create(
            domain="public.localhost", tenant=public_tenant, is_primary=True
        )
        cls.password = "ClaveSegura123"
        cls.billing_staff = PlatformStaff.objects.create(
            email="billing@fivuza.com", full_name="Billing", role="BILLING"
        )
        cls.billing_staff.set_password(cls.password)
        cls.billing_staff.save()
        cls.support_staff = PlatformStaff.objects.create(
            email="support@fivuza.com", full_name="Support", role="SUPPORT"
        )
        cls.support_staff.set_password(cls.password)
        cls.support_staff.save()

        cls.tenant = Tenant.objects.create(
            schema_name="test_payment_confirm", company_name="Negocio con Pago"
        )
        cls.plan = Plan.objects.create(
            code="PLAN_CONFIRM",
            name="Plan Confirm",
            max_users=1,
            price_monthly=39,
            price_semiannual=195,
            price_annual=390,
        )

    def setUp(self):
        from core.models import Subscription, SubscriptionPayment

        self.starts_at = timezone.now()
        self.expires_at = self.starts_at + timedelta(days=30)
        self.subscription = Subscription.objects.create(
            tenant=self.tenant,
            plan=self.plan,
            billing_cycle="MONTHLY",
            price_paid=39,
            status="active",
            starts_at=self.starts_at,
            expires_at=self.expires_at,
        )
        self.payment = SubscriptionPayment.objects.create(
            subscription=self.subscription,
            amount=39,
            payment_method="TRANSFER",
            status="PENDING",
        )

    def _client_as(self, staff):
        client = APIClient(HTTP_HOST="public.localhost")
        login = client.post(
            "/api/v1/platform/auth/login/",
            {"email": staff.email, "password": self.password},
            format="json",
        )
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {login.data['access']}")
        return client

    def test_billing_can_confirm_payment(self):
        response = self._client_as(self.billing_staff).post(
            f"/api/v1/core/subscription-payments/{self.payment.id}/confirm/",
            {},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], "PAID")
        self.assertIsNotNone(response.data["paid_at"])

        self.subscription.refresh_from_db()
        self.assertEqual(
            self.subscription.expires_at, self.expires_at + timedelta(days=30)
        )

    def test_confirming_extends_a_past_due_subscription_from_now(self):
        self.subscription.status = "past_due"
        self.subscription.expires_at = timezone.now() - timedelta(days=10)
        self.subscription.save(update_fields=["status", "expires_at"])

        response = self._client_as(self.billing_staff).post(
            f"/api/v1/core/subscription-payments/{self.payment.id}/confirm/",
            {},
            format="json",
        )
        self.assertEqual(response.status_code, 200)

        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.status, "active")
        self.assertGreater(
            self.subscription.expires_at, timezone.now() + timedelta(days=29)
        )

    def test_support_role_cannot_confirm_payment(self):
        response = self._client_as(self.support_staff).post(
            f"/api/v1/core/subscription-payments/{self.payment.id}/confirm/",
            {},
            format="json",
        )
        self.assertEqual(response.status_code, 403)

    def test_confirming_twice_is_rejected(self):
        client = self._client_as(self.billing_staff)
        client.post(
            f"/api/v1/core/subscription-payments/{self.payment.id}/confirm/",
            {},
            format="json",
        )
        response = client.post(
            f"/api/v1/core/subscription-payments/{self.payment.id}/confirm/",
            {},
            format="json",
        )
        self.assertEqual(response.status_code, 409)

    def test_confirm_writes_audit_log(self):
        from core.models import PlatformAuditLog

        self._client_as(self.billing_staff).post(
            f"/api/v1/core/subscription-payments/{self.payment.id}/confirm/",
            {},
            format="json",
        )
        self.assertTrue(
            PlatformAuditLog.objects.filter(
                action="PAYMENT_CONFIRMED", entity_id=self.payment.id
            ).exists()
        )

    def test_payments_filtered_by_subscription(self):
        from core.models import Subscription, SubscriptionPayment

        other_subscription = Subscription.objects.create(
            tenant=Tenant.objects.create(
                schema_name="test_other_sub", company_name="Otro Negocio"
            ),
            plan=self.plan,
            billing_cycle="MONTHLY",
            price_paid=39,
            status="active",
            starts_at=self.starts_at,
            expires_at=self.expires_at,
        )
        SubscriptionPayment.objects.create(
            subscription=other_subscription,
            amount=39,
            payment_method="TRANSFER",
            status="PENDING",
        )

        response = self._client_as(self.billing_staff).get(
            f"/api/v1/core/subscription-payments/?subscription={self.subscription.id}"
        )
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["id"], self.payment.id)

    def test_subscriptions_filtered_by_tenant(self):
        response = self._client_as(self.billing_staff).get(
            f"/api/v1/core/subscriptions/?tenant={self.tenant.id}"
        )
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["id"], self.subscription.id)


class TenantFeatureOverrideTests(APITestCase):
    """PATCH/DELETE /core/tenants/{id}/feature-overrides/{feature_code}/
    (Sprint 10, Especificacion de API §4.25). Solo SUPER_ADMIN escribe."""

    @classmethod
    def setUpTestData(cls):
        public_tenant = Tenant.objects.create(
            schema_name="public", company_name="Servicio Publico"
        )
        Domain.objects.create(
            domain="public.localhost", tenant=public_tenant, is_primary=True
        )
        cls.password = "ClaveSegura123"
        cls.super_admin = PlatformStaff.objects.create(
            email="admin@fivuza.com", full_name="Super Admin", role="SUPER_ADMIN"
        )
        cls.super_admin.set_password(cls.password)
        cls.super_admin.save()
        cls.support = PlatformStaff.objects.create(
            email="soporte@fivuza.com", full_name="Soporte", role="SUPPORT"
        )
        cls.support.set_password(cls.password)
        cls.support.save()
        cls.tenant = Tenant.objects.create(
            schema_name="test_feature_override", company_name="Negocio Beta"
        )

    def _client_as(self, staff):
        client = APIClient(HTTP_HOST="public.localhost")
        login = client.post(
            "/api/v1/platform/auth/login/",
            {"email": staff.email, "password": self.password},
            format="json",
        )
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {login.data['access']}")
        return client

    def test_support_cannot_set_override(self):
        response = self._client_as(self.support).patch(
            f"/api/v1/core/tenants/{self.tenant.id}/feature-overrides/HAS_MULTI_WAREHOUSE/",
            {"is_enabled": True},
            format="json",
        )
        self.assertEqual(response.status_code, 403)

    def test_super_admin_can_force_enable_feature_plan_does_not_include(self):
        from core.services import FeatureFlagService

        response = self._client_as(self.super_admin).patch(
            f"/api/v1/core/tenants/{self.tenant.id}/feature-overrides/HAS_MULTI_WAREHOUSE/",
            {"is_enabled": True},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["is_enabled"], True)
        self.assertTrue(
            FeatureFlagService.is_enabled(self.tenant, "HAS_MULTI_WAREHOUSE")
        )

    def test_override_is_listed_and_can_be_removed(self):
        client = self._client_as(self.super_admin)
        client.patch(
            f"/api/v1/core/tenants/{self.tenant.id}/feature-overrides/HAS_HR_MODULE/",
            {"is_enabled": True},
            format="json",
        )

        listed = client.get(f"/api/v1/core/tenants/{self.tenant.id}/feature-overrides/")
        self.assertEqual(len(listed.data), 1)
        self.assertEqual(listed.data[0]["feature_code"], "HAS_HR_MODULE")

        deleted = client.delete(
            f"/api/v1/core/tenants/{self.tenant.id}/feature-overrides/HAS_HR_MODULE/"
        )
        self.assertEqual(deleted.status_code, 204)

        listed_after = client.get(
            f"/api/v1/core/tenants/{self.tenant.id}/feature-overrides/"
        )
        self.assertEqual(len(listed_after.data), 0)

    def test_setting_override_writes_audit_log(self):
        from core.models import PlatformAuditLog

        self._client_as(self.super_admin).patch(
            f"/api/v1/core/tenants/{self.tenant.id}/feature-overrides/HAS_CASH_MODULE/",
            {"is_enabled": False},
            format="json",
        )
        self.assertTrue(
            PlatformAuditLog.objects.filter(
                action="TENANT_FEATURE_OVERRIDE_SET", entity_id=self.tenant.id
            ).exists()
        )


class DashboardSummaryViewTests(APITestCase):
    """GET /api/v1/core/dashboard/summary/ (Especificacion de API §4.13)."""

    @classmethod
    def setUpTestData(cls):
        public_tenant = Tenant.objects.create(
            schema_name="public", company_name="Servicio Publico"
        )
        Domain.objects.create(
            domain="public.localhost", tenant=public_tenant, is_primary=True
        )
        cls.password = "ClaveSegura123"
        cls.super_admin = PlatformStaff.objects.create(
            email="admin@fivuza.com", full_name="Super Admin", role="SUPER_ADMIN"
        )
        cls.super_admin.set_password(cls.password)
        cls.super_admin.save()

        from core.models import Plan, Subscription

        cls.active_tenant = Tenant.objects.create(
            schema_name="test_dash_active", company_name="Activo", status="active"
        )
        cls.suspended_tenant = Tenant.objects.create(
            schema_name="test_dash_suspended",
            company_name="Suspendido",
            status="suspended",
        )
        cls.canceled_tenant = Tenant.objects.create(
            schema_name="test_dash_canceled",
            company_name="Cancelado",
            status="canceled",
            canceled_at="2026-07-15T09:00:00Z",
        )
        cls.plan = Plan.objects.create(
            code="PLAN_DASH",
            name="Plan Dash",
            max_users=1,
            price_monthly=30,
            price_semiannual=150,
            price_annual=300,
        )
        cls.subscription = Subscription.objects.create(
            tenant=cls.active_tenant,
            plan=cls.plan,
            billing_cycle="ANNUAL",
            price_paid=300,
            status="active",
            starts_at="2026-01-01T00:00:00Z",
            expires_at="2027-01-01T00:00:00Z",
        )

    def _client_as(self, staff):
        client = APIClient(HTTP_HOST="public.localhost")
        login = client.post(
            "/api/v1/platform/auth/login/",
            {"email": staff.email, "password": self.password},
            format="json",
        )
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {login.data['access']}")
        return client

    def test_summary_requires_platform_staff(self):
        response = APIClient(HTTP_HOST="public.localhost").get(
            "/api/v1/core/dashboard/summary/"
        )
        self.assertEqual(response.status_code, 401)

    def test_summary_aggregates_expected_fields(self):
        response = self._client_as(self.super_admin).get(
            "/api/v1/core/dashboard/summary/"
        )
        self.assertEqual(response.status_code, 200)
        data = response.data
        self.assertEqual(data["tenants_by_status"]["active"], 1)
        self.assertEqual(data["tenants_by_status"]["suspended"], 1)
        self.assertEqual(data["tenants_by_status"]["canceled"], 1)
        self.assertEqual(float(data["mrr"]), 25.0)  # 300 ANNUAL / 12
        self.assertEqual(data["pending_payments_count"], 0)
        self.assertEqual(len(data["recently_suspended"]), 1)
        self.assertEqual(len(data["recently_canceled"]), 1)
        canceled_row = data["recently_canceled"][0]
        self.assertEqual(canceled_row["id"], self.canceled_tenant.id)
        self.assertIsNotNone(canceled_row["data_retention_until"])


class ApiDocsAccessTests(APITestCase):
    """La documentacion de la API (schema/Swagger/ReDoc) no debe quedar
    publica en produccion -solo platform_staff autenticado (Sprint 7,
    endurecimiento de produccion)."""

    @classmethod
    def setUpTestData(cls):
        public_tenant = Tenant.objects.create(
            schema_name="public", company_name="Servicio Publico"
        )
        Domain.objects.create(
            domain="public.localhost", tenant=public_tenant, is_primary=True
        )
        cls.password = "ClaveSegura123"
        cls.staff = PlatformStaff.objects.create(
            email="admin@fivuza.com", full_name="Admin Fivuza", role="SUPER_ADMIN"
        )
        cls.staff.set_password(cls.password)
        cls.staff.save()

    def _client_as_staff(self):
        client = APIClient(HTTP_HOST="public.localhost")
        login = client.post(
            "/api/v1/platform/auth/login/",
            {"email": self.staff.email, "password": self.password},
            format="json",
        )
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {login.data['access']}")
        return client

    def test_schema_anonymous_is_rejected(self):
        response = APIClient(HTTP_HOST="public.localhost").get("/api/schema/")
        self.assertEqual(response.status_code, 401)

    def test_swagger_ui_anonymous_is_rejected(self):
        response = APIClient(HTTP_HOST="public.localhost").get("/api/docs/")
        self.assertEqual(response.status_code, 401)

    def test_redoc_anonymous_is_rejected(self):
        response = APIClient(HTTP_HOST="public.localhost").get("/api/redoc/")
        self.assertEqual(response.status_code, 401)

    def test_schema_platform_staff_is_allowed(self):
        response = self._client_as_staff().get("/api/schema/")
        self.assertEqual(response.status_code, 200)
