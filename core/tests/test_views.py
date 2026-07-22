# Pruebas de ViewSets/vistas: permisos, serialización, códigos de respuesta HTTP.
from rest_framework.test import APIClient, APIRequestFactory, APITestCase

from core.models import Domain, PlatformStaff, Tenant
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
            "/api/v1/auth/platform/login/",
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
            "/api/v1/auth/platform/login/",
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
            "/api/v1/auth/platform/logout/", {"refresh": "x"}, format="json"
        )
        self.assertEqual(response.status_code, 401)

    def test_refresh_then_logout_blacklists_refresh_token(self):
        tokens = self._login().data
        access, refresh = tokens["access"], tokens["refresh"]

        refresh_response = self.client.post(
            "/api/v1/auth/platform/refresh/", {"refresh": refresh}, format="json"
        )
        self.assertEqual(refresh_response.status_code, 200)

        old_refresh_reuse = self.client.post(
            "/api/v1/auth/platform/refresh/", {"refresh": refresh}, format="json"
        )
        self.assertEqual(old_refresh_reuse.status_code, 401)

        new_refresh = refresh_response.data["refresh"]
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
        logout_response = self.client.post(
            "/api/v1/auth/platform/logout/", {"refresh": new_refresh}, format="json"
        )
        self.assertEqual(logout_response.status_code, 205)

        reuse_after_logout = self.client.post(
            "/api/v1/auth/platform/refresh/", {"refresh": new_refresh}, format="json"
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
            "/api/v1/auth/platform/login/",
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
