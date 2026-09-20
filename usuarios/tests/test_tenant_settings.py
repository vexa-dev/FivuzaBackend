# Bloque A.0: el propio negocio edita sus interruptores operativos desde el
# ERP, sin depender del panel interno de Fivuza.
from django.core.cache import cache
from django_tenants.test.cases import TenantTestCase
from rest_framework.test import APIClient

from core.models import TenantSettings
from usuarios.models import AuditLog, Role, User


class TenantOperationalSettingsTests(TenantTestCase):
    @classmethod
    def get_test_schema_name(cls):
        return "test_usuarios_tenant_settings"

    @classmethod
    def get_test_tenant_domain(cls):
        return "test-usuarios-tenant-settings.test.com"

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
        TenantSettings.objects.filter(tenant=self.tenant).update(
            cashier_can_open_session=False, cashier_can_close_session=False
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

    def test_admin_reads_the_operational_switches(self):
        response = self._client_as(self.admin_user).get("/api/v1/usuarios/settings/")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data["cashier_can_open_session"])
        self.assertFalse(response.data["cashier_can_close_session"])

    def test_admin_updates_a_switch(self):
        response = self._client_as(self.admin_user).patch(
            "/api/v1/usuarios/settings/",
            {"cashier_can_open_session": True},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["cashier_can_open_session"])

        settings_row = TenantSettings.objects.get(tenant=self.tenant)
        self.assertTrue(settings_row.cashier_can_open_session)
        self.assertFalse(settings_row.cashier_can_close_session)

    def test_seller_cannot_read_or_update_settings(self):
        client = self._client_as(self.seller_user)
        self.assertEqual(client.get("/api/v1/usuarios/settings/").status_code, 403)
        self.assertEqual(
            client.patch(
                "/api/v1/usuarios/settings/",
                {"cashier_can_open_session": True},
                format="json",
            ).status_code,
            403,
        )

    def test_endpoint_does_not_expose_plan_or_billing_switches(self):
        """Los modulos contratados y el umbral de alerta los decide Fivuza,
        no el tenant -si aparecieran aqui, el negocio podria prenderse
        modulos que no pago."""
        response = self._client_as(self.admin_user).get("/api/v1/usuarios/settings/")
        for field in (
            "purchases_enabled",
            "hr_module_enabled",
            "gym_module_enabled",
            "cash_difference_alert_threshold",
        ):
            self.assertNotIn(field, response.data)

    def test_ignores_fields_outside_the_operational_switches(self):
        self._client_as(self.admin_user).patch(
            "/api/v1/usuarios/settings/",
            {"cashier_can_open_session": True, "gym_module_enabled": True},
            format="json",
        )
        settings_row = TenantSettings.objects.get(tenant=self.tenant)
        self.assertTrue(settings_row.cashier_can_open_session)
        self.assertFalse(settings_row.gym_module_enabled)

    def test_change_is_audited_with_before_and_after(self):
        self._client_as(self.admin_user).patch(
            "/api/v1/usuarios/settings/",
            {"cashier_can_close_session": True},
            format="json",
        )
        log = AuditLog.objects.filter(action="TENANT_SETTINGS_UPDATED").first()
        self.assertIsNotNone(log)
        self.assertIn("cashier_can_close_session", log.details)

    def test_no_change_writes_no_audit_log(self):
        self._client_as(self.admin_user).patch(
            "/api/v1/usuarios/settings/",
            {"cashier_can_open_session": False},
            format="json",
        )
        self.assertFalse(
            AuditLog.objects.filter(action="TENANT_SETTINGS_UPDATED").exists()
        )

    def test_turning_a_switch_off_takes_effect_on_the_next_request(self):
        """El interruptor concede permisos: si el cache sobrevive al cambio,
        el cajero sigue entrando durante minutos (Bloque A.1.5)."""
        from usuarios.services import PermissionService

        client = self._client_as(self.admin_user)
        client.patch(
            "/api/v1/usuarios/settings/",
            {"cashier_can_open_session": True},
            format="json",
        )
        self.assertIn(
            "CASH_OPEN",
            PermissionService.get_effective_permission_codes(self.seller_user),
        )

        client.patch(
            "/api/v1/usuarios/settings/",
            {"cashier_can_open_session": False},
            format="json",
        )
        self.assertNotIn(
            "CASH_OPEN",
            PermissionService.get_effective_permission_codes(self.seller_user),
        )

    def test_platform_panel_also_clears_the_permission_cache(self):
        """Fivuza puede tocar los mismos interruptores desde su panel
        interno, que corre en el esquema public: la invalidacion tiene que
        apuntar al esquema del tenant, no al de la request."""
        from django.db import connection

        from usuarios.services import PermissionService

        settings_row = TenantSettings.objects.get(tenant=self.tenant)
        settings_row.cashier_can_open_session = True
        settings_row.save(update_fields=["cashier_can_open_session"])
        PermissionService.invalidate_cashier_switches_cache(connection.schema_name)
        self.assertIn(
            "CASH_OPEN",
            PermissionService.get_effective_permission_codes(self.seller_user),
        )

        # Simula el guardado desde el panel: la fila cambia sin que la
        # conexion este dentro del esquema del tenant.
        TenantSettings.objects.filter(tenant=self.tenant).update(
            cashier_can_open_session=False
        )
        PermissionService.invalidate_cashier_switches_cache(self.tenant.schema_name)
        self.assertNotIn(
            "CASH_OPEN",
            PermissionService.get_effective_permission_codes(self.seller_user),
        )
