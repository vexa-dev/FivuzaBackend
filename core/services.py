from datetime import timedelta

from django.utils import timezone

from core.models import Domain, Plan, Subscription, Tenant, TenantSettings

_BILLING_CYCLE_DAYS = {"MONTHLY": 30, "SEMIANNUAL": 182, "ANNUAL": 365}
_BILLING_CYCLE_PRICE_FIELD = {
    "MONTHLY": "price_monthly",
    "SEMIANNUAL": "price_semiannual",
    "ANNUAL": "price_annual",
}


class TenantRegistrationService:
    """Registro de un tenant nuevo (Especificacion de API §4.9).

    Crea Tenant + Domain + Subscription (snapshot del precio del plan segun
    billing_cycle) de forma sincrona -el esquema fisico ya lo crea
    django-tenants dentro de Tenant.save() (auto_create_schema=True), y
    TenantProvisioningService se dispara solo via el signal post_save.

    Nota: la Especificacion de API describe esto como asincrono via Celery
    (TRD §5.4) para que la respuesta HTTP no espere el aprovisionamiento
    completo. Se deja sincrono por ahora -el aprovisionamiento actual
    (TenantSettings) es una escritura trivial, no amerita todavia la
    complejidad de una tarea de Celery con seguimiento de estado. Se
    revisita cuando el aprovisionamiento real (roles/almacen/caja) se
    implemente en Sprint 2-3 y sea lo bastante pesado como para justificarlo.
    """

    @staticmethod
    def register(
        *,
        company_name: str,
        ruc: str,
        schema_name: str,
        domain: str,
        plan_code: str,
        billing_cycle: str,
    ) -> Tenant:
        plan = Plan.objects.get(code=plan_code, is_active=True)

        tenant = Tenant.objects.create(
            schema_name=schema_name, company_name=company_name, ruc=ruc
        )
        Domain.objects.create(domain=domain, tenant=tenant, is_primary=True)

        price_field = _BILLING_CYCLE_PRICE_FIELD[billing_cycle]
        starts_at = timezone.now()
        Subscription.objects.create(
            tenant=tenant,
            plan=plan,
            billing_cycle=billing_cycle,
            price_paid=getattr(plan, price_field),
            status="active",
            starts_at=starts_at,
            expires_at=starts_at + timedelta(days=_BILLING_CYCLE_DAYS[billing_cycle]),
        )
        return tenant


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
