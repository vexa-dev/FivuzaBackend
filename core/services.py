from core.models import Tenant, TenantSettings


class TenantProvisioningService:
    """Aprovisiona un tenant nuevo apenas se crea.

    Version inicial (Sprint 1, Plan de Implementacion): solo crea el registro
    1:1 de TenantSettings. El esquema fisico en Postgres ya lo crea
    django-tenants automaticamente (Tenant.auto_create_schema = True).
    La creacion de roles por defecto, almacen 'Principal' y caja por defecto
    se completa en Sprint 2-3, cuando esos modelos ya tengan su catalogo base
    (permisos, etc.) listo para poblarlos.
    """

    @staticmethod
    def provision(tenant: Tenant) -> TenantSettings:
        settings, _ = TenantSettings.objects.get_or_create(tenant=tenant)
        return settings
