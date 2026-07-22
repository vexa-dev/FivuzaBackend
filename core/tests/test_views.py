# Pruebas de ViewSets/vistas: permisos, serialización, códigos de respuesta HTTP.
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
        self.assertEqual(response.data["provisioning_status"], "IN_PROGRESS")

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
