# Pruebas de flujo completo a través de la capa de servicios (ej. crear una venta
# de punta a punta), no solo de una unidad aislada.
from django.test import TestCase

from core.models import Tenant, TenantSettings


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
