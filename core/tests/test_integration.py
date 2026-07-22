# Pruebas de flujo completo a través de la capa de servicios (ej. crear una venta
# de punta a punta), no solo de una unidad aislada.
from django.test import TestCase
from django_tenants.test.cases import TenantTestCase
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.test import APIRequestFactory
from rest_framework_simplejwt.tokens import AccessToken

from core.authentication import (
    PlatformStaffJWTAuthentication,
    TenantValidatedJWTAuthentication,
)
from core.models import PlatformStaff, Tenant, TenantSettings


class TenantProvisioningServiceTests(TestCase):
    def test_creating_a_tenant_auto_provisions_tenant_settings(self):
        tenant = Tenant.objects.create(
            schema_name="test_provisioning", company_name="Negocio de Prueba"
        )

        settings = TenantSettings.objects.get(tenant=tenant)

        self.assertTrue(settings.purchases_enabled)
        self.assertTrue(settings.cash_module_enabled)
        self.assertFalse(settings.variants_enabled)
        self.assertFalse(settings.multi_warehouse_enabled)
        self.assertFalse(settings.hr_module_enabled)

    def test_provisioning_is_idempotent(self):
        tenant = Tenant.objects.create(
            schema_name="test_provisioning_2", company_name="Negocio de Prueba 2"
        )

        from core.services import TenantProvisioningService

        TenantProvisioningService.provision(tenant)

        self.assertEqual(TenantSettings.objects.filter(tenant=tenant).count(), 1)


class TenantValidatedJWTAuthenticationTests(TenantTestCase):
    """No existe todavia un endpoint de login de tenant.users (llega en
    Sprint 2), asi que se prueba la clase de autenticacion directamente,
    construyendo el token a mano -exactamente lo que hara ese endpoint."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from usuarios.models import Role, User

        cls.role = Role.objects.create(name="admin", is_system_default=True)
        cls.user = User.objects.create(email="vendedor@negocio.com", role=cls.role)

    @classmethod
    def tearDownClass(cls):
        # TenantProvisioningService (signal post_save de Tenant) crea un
        # TenantSettings PROTECT para el tenant de prueba de TenantTestCase;
        # hay que limpiarlo antes de que el propio TenantTestCase borre el tenant.
        TenantSettings.objects.filter(tenant=cls.tenant).delete()
        super().tearDownClass()

    def _authenticate(self, schema_name):
        token = AccessToken()
        token["user_id"] = self.user.id
        token["schema_name"] = schema_name

        request = APIRequestFactory().get("/")
        request.tenant = self.tenant
        request.META["HTTP_AUTHORIZATION"] = f"Bearer {token}"

        return TenantValidatedJWTAuthentication().authenticate(request)

    def test_token_with_matching_schema_authenticates(self):
        user, validated_token = self._authenticate(self.tenant.schema_name)
        self.assertEqual(user.id, self.user.id)
        self.assertEqual(validated_token["schema_name"], self.tenant.schema_name)

    def test_token_with_mismatched_schema_is_rejected(self):
        with self.assertRaises(AuthenticationFailed) as ctx:
            self._authenticate("otro_tenant")
        self.assertEqual(ctx.exception.detail["code"], "tenant_mismatch")

    def test_token_for_inactive_user_is_rejected(self):
        self.user.is_active = False
        self.user.save()
        with self.assertRaises(AuthenticationFailed) as ctx:
            self._authenticate(self.tenant.schema_name)
        self.assertEqual(ctx.exception.detail["code"], "user_not_found")
        self.user.is_active = True
        self.user.save()

    def test_platform_staff_authenticator_ignores_tenant_tokens_even_with_colliding_id(
        self,
    ):
        # Un platform_staff con el MISMO id que self.user existe a proposito:
        # si PlatformStaffJWTAuthentication no reconociera el claim
        # schema_name, autenticaria por error al miembro del equipo Fivuza
        # en vez de dejar pasar el token al TenantValidatedJWTAuthentication.
        from django_tenants.utils import schema_context

        with schema_context("public"):
            PlatformStaff.objects.create(
                id=self.user.id,
                email="coincidencia@fivuza.com",
                full_name="Coincidencia de ID",
                role="SUPPORT",
            )

        token = AccessToken()
        token["user_id"] = self.user.id
        token["schema_name"] = self.tenant.schema_name

        request = APIRequestFactory().get("/")
        request.tenant = self.tenant
        request.META["HTTP_AUTHORIZATION"] = f"Bearer {token}"

        self.assertIsNone(PlatformStaffJWTAuthentication().authenticate(request))
