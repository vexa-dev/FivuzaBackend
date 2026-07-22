from django.utils import timezone

from core.models import Tenant, TenantSettings


class TenantLifecycleService:
    """Suspende/reactiva el acceso de un tenant sin borrar ningun dato -una
    suspension es una pausa, no una eliminacion (Especificacion de API,
    seccion 4.12). Solo accesible por platform_staff.

    El motivo (reason) de la suspension no tiene un campo propio en la BDD
    v5 -se acepta como parametro para uso futuro (ej. bitacora de soporte),
    pero por ahora no se persiste en ningun lado.
    """

    @staticmethod
    def suspend_tenant(tenant: Tenant, reason: str | None = None) -> Tenant:
        tenant.status = "suspended"
        tenant.suspended_at = timezone.now()
        tenant.save(update_fields=["status", "suspended_at"])
        return tenant

    @staticmethod
    def reactivate_tenant(tenant: Tenant) -> Tenant:
        tenant.status = "active"
        tenant.suspended_at = None
        tenant.save(update_fields=["status", "suspended_at"])
        return tenant


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
