# Pruebas de flujo completo a través de la capa de servicios (ej. crear una venta
# de punta a punta), no solo de una unidad aislada.
from django.core.cache import cache
from django.test import TestCase
from django_tenants.utils import get_public_schema_name, schema_context

from core.models import Domain, Tenant


class PermissionServiceCacheScopingTests(TestCase):
    """usuarios.User.id no es globalmente unico -cada esquema de tenant
    tiene su propia secuencia autoincremental (Sprint 10, encontrado al
    escribir las pruebas de impersonacion: dos tenants nuevos, cada uno con
    su primer usuario en id=1, con permisos distintos). El cache de
    PermissionService debe estar aislado por schema_name, no solo por
    user_id."""

    def setUp(self):
        cache.clear()

    def test_same_user_id_in_different_tenants_does_not_leak_permissions(self):
        from usuarios.models import Role, User
        from usuarios.services import PermissionService

        with schema_context(get_public_schema_name()):
            tenant_a = Tenant.objects.create(
                schema_name="test_cache_scope_a", company_name="Negocio A"
            )
            Domain.objects.create(
                domain="test-cache-scope-a.test.com", tenant=tenant_a, is_primary=True
            )
            tenant_b = Tenant.objects.create(
                schema_name="test_cache_scope_b", company_name="Negocio B"
            )
            Domain.objects.create(
                domain="test-cache-scope-b.test.com", tenant=tenant_b, is_primary=True
            )

        with schema_context(tenant_a.schema_name):
            admin_role = Role.objects.get(name="admin")
            user_a = User.objects.create(email="admin@a.com", role=admin_role)
            self.assertEqual(user_a.id, 1)
            codes_a = PermissionService.get_permission_codes(user_a)
            self.assertIn("INVENTORY_MANAGE", codes_a)

        with schema_context(tenant_b.schema_name):
            seller_role = Role.objects.get(name="seller")
            user_b = User.objects.create(email="seller@b.com", role=seller_role)
            self.assertEqual(user_b.id, 1)
            codes_b = PermissionService.get_permission_codes(user_b)
            # Si el cache colisionara por id (ignorando el schema), aca
            # llegaria el resultado ya cacheado de user_a (admin) en vez del
            # de este usuario (seller, mucho mas acotado).
            self.assertNotIn("INVENTORY_MANAGE", codes_b)
            self.assertEqual(
                codes_b, {"INVENTORY_VIEW", "SALES_MANAGE", "SALES_RETURN"}
            )
