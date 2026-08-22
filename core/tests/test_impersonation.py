# Sesiones de impersonacion de tenant (Sprint 10, Especificacion de API §4.24).
from django.core.cache import cache
from django.utils import timezone
from django_tenants.utils import schema_context
from rest_framework.test import APIClient, APITestCase

from core.models import Domain, PlatformStaff, Tenant, TenantImpersonationSession


class TenantImpersonationTests(APITestCase):
    def setUp(self):
        # PermissionService cachea por (schema_name, user_id) -sin esto,
        # el cache de un tenant creado en otro test de esta misma corrida
        # podria seguir vivo (TTL de 5 min) y devolver un resultado stale.
        cache.clear()

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
        cls.billing = PlatformStaff.objects.create(
            email="facturacion@fivuza.com", full_name="Billing", role="BILLING"
        )
        cls.billing.set_password(cls.password)
        cls.billing.save()

        cls.target_tenant = Tenant.objects.create(
            schema_name="test_impersonation", company_name="Negocio Impersonado"
        )
        Domain.objects.create(
            domain="test-impersonation.test.com",
            tenant=cls.target_tenant,
            is_primary=True,
        )
        with schema_context(cls.target_tenant.schema_name):
            from usuarios.models import Role, User

            cls.admin_role = Role.objects.get(name="admin")
            cls.tenant_admin = User.objects.create(
                email="admin@negocio.com", role=cls.admin_role
            )
            cls.tenant_admin.set_password("otra-clave")
            cls.tenant_admin.save()

        cls.empty_tenant = Tenant.objects.create(
            schema_name="test_impersonation_empty", company_name="Negocio Sin Admin"
        )

    def _client_as_staff(self, staff):
        client = APIClient(HTTP_HOST="public.localhost")
        login = client.post(
            "/api/v1/platform/auth/login/",
            {"email": staff.email, "password": self.password},
            format="json",
        )
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {login.data['access']}")
        return client

    def test_super_admin_can_start_impersonation(self):
        response = self._client_as_staff(self.super_admin).post(
            f"/api/v1/core/tenants/{self.target_tenant.id}/impersonation/",
            {"reason": "Cliente reporta un problema, se revisa en vivo"},
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        self.assertIn("access_token", response.data)
        self.assertIn("session_id", response.data)
        self.assertIn("expires_at", response.data)
        self.assertEqual(response.data["user"]["email"], self.tenant_admin.email)
        self.assertEqual(response.data["user"]["role"], "admin")
        self.assertIn("INVENTORY_MANAGE", response.data["user"]["permissions"])

        session = TenantImpersonationSession.objects.get(id=response.data["session_id"])
        self.assertEqual(session.tenant, self.target_tenant)
        self.assertEqual(session.platform_staff, self.super_admin)
        self.assertIsNone(session.ended_at)

    def test_billing_cannot_start_impersonation(self):
        response = self._client_as_staff(self.billing).post(
            f"/api/v1/core/tenants/{self.target_tenant.id}/impersonation/",
            {"reason": "x"},
            format="json",
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data["error"]["code"], "PERMISSION_DENIED")

    def test_impersonating_tenant_without_admin_user_fails(self):
        response = self._client_as_staff(self.super_admin).post(
            f"/api/v1/core/tenants/{self.empty_tenant.id}/impersonation/",
            {"reason": "x"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["error"]["code"], "NO_ADMIN_USER")

    def test_impersonation_token_authenticates_as_tenant_admin(self):
        start = self._client_as_staff(self.super_admin).post(
            f"/api/v1/core/tenants/{self.target_tenant.id}/impersonation/",
            {"reason": "x"},
            format="json",
        )
        tenant_client = APIClient(HTTP_HOST=self.target_tenant.domains.get().domain)
        tenant_client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {start.data['access_token']}"
        )
        response = tenant_client.get("/api/v1/inventario/categories/")
        self.assertEqual(response.status_code, 200)

    def test_starting_impersonation_writes_both_audit_logs(self):
        from core.models import PlatformAuditLog
        from usuarios.models import AuditLog

        response = self._client_as_staff(self.super_admin).post(
            f"/api/v1/core/tenants/{self.target_tenant.id}/impersonation/",
            {"reason": "Revision en vivo"},
            format="json",
        )
        self.assertTrue(
            PlatformAuditLog.objects.filter(
                action="TENANT_IMPERSONATION_STARTED", entity_id=self.target_tenant.id
            ).exists()
        )
        with schema_context(self.target_tenant.schema_name):
            log = AuditLog.objects.get(
                action="SUPPORT_IMPERSONATION_STARTED",
                entity_id=response.data["session_id"],
            )
            self.assertIn(self.super_admin.email, log.details)

    def test_staff_can_end_session_early_and_token_is_revoked(self):
        client = self._client_as_staff(self.super_admin)
        start = client.post(
            f"/api/v1/core/tenants/{self.target_tenant.id}/impersonation/",
            {"reason": "x"},
            format="json",
        )
        session_id = start.data["session_id"]

        end = client.delete(
            f"/api/v1/core/tenants/{self.target_tenant.id}/impersonation/{session_id}/"
        )
        self.assertEqual(end.status_code, 204)

        tenant_client = APIClient(HTTP_HOST=self.target_tenant.domains.get().domain)
        tenant_client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {start.data['access_token']}"
        )
        response = tenant_client.get("/api/v1/inventario/categories/")
        self.assertEqual(response.status_code, 401)
        self.assertIn("termino", str(response.data["error"]["message"]))

    def test_impersonated_user_can_end_own_session_from_erp(self):
        start = self._client_as_staff(self.super_admin).post(
            f"/api/v1/core/tenants/{self.target_tenant.id}/impersonation/",
            {"reason": "x"},
            format="json",
        )
        tenant_client = APIClient(HTTP_HOST=self.target_tenant.domains.get().domain)
        tenant_client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {start.data['access_token']}"
        )

        end = tenant_client.post("/api/v1/impersonation/end/", {}, format="json")
        self.assertEqual(end.status_code, 205)

        session = TenantImpersonationSession.objects.get(id=start.data["session_id"])
        self.assertIsNotNone(session.ended_at)

        response = tenant_client.get("/api/v1/inventario/categories/")
        self.assertEqual(response.status_code, 401)

    def test_regular_tenant_token_cannot_self_end_impersonation(self):
        # Un login normal (no impersonado) no tiene el claim
        # impersonation_session_id -no hay nada que terminar.
        client = APIClient(HTTP_HOST=self.target_tenant.domains.get().domain)
        login = client.post(
            "/api/v1/auth/login/",
            {"email": self.tenant_admin.email, "password": "otra-clave"},
            format="json",
        )
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {login.data['access']}")
        response = client.post("/api/v1/impersonation/end/", {}, format="json")
        self.assertEqual(response.status_code, 400)

    def test_action_during_impersonation_is_marked_in_tenant_audit_log(self):
        from usuarios.models import AuditLog, Permission, Role

        with schema_context(self.target_tenant.schema_name):
            seller_role = Role.objects.get(name="seller")
            permission = Permission.objects.get(code="INVENTORY_MANAGE")

        start = self._client_as_staff(self.super_admin).post(
            f"/api/v1/core/tenants/{self.target_tenant.id}/impersonation/",
            {"reason": "x"},
            format="json",
        )
        tenant_client = APIClient(HTTP_HOST=self.target_tenant.domains.get().domain)
        tenant_client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {start.data['access_token']}"
        )
        response = tenant_client.post(
            "/api/v1/usuarios/role-permissions/",
            {"role": seller_role.id, "permission": permission.id},
            format="json",
        )
        self.assertEqual(response.status_code, 201)

        with schema_context(self.target_tenant.schema_name):
            log = AuditLog.objects.get(
                action="USER_ROLE_CHANGED", entity_id=seller_role.id
            )
            self.assertIn("accion de soporte Fivuza", log.details)
            self.assertIn(str(self.super_admin.id), log.details)

    def test_session_expires_after_60_minutes(self):
        start = self._client_as_staff(self.super_admin).post(
            f"/api/v1/core/tenants/{self.target_tenant.id}/impersonation/",
            {"reason": "x"},
            format="json",
        )
        session = TenantImpersonationSession.objects.get(id=start.data["session_id"])
        session.expires_at = timezone.now() - timezone.timedelta(minutes=1)
        session.save(update_fields=["expires_at"])

        tenant_client = APIClient(HTTP_HOST=self.target_tenant.domains.get().domain)
        tenant_client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {start.data['access_token']}"
        )
        response = tenant_client.get("/api/v1/inventario/categories/")
        self.assertEqual(response.status_code, 401)
