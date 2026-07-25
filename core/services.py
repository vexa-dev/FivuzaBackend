import json
from datetime import timedelta

from django.db.models import Count
from django.utils import timezone

from core.models import (
    Domain,
    Plan,
    PlatformAuditLog,
    PlatformStaff,
    Subscription,
    SubscriptionPayment,
    Tenant,
    TenantSettings,
)

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


class PlatformAuditLogService:
    """Unico punto de entrada para escribir en platform_audit_logs (BDD v5,
    seccion public.platform_audit_logs). Las vistas de core llaman a
    log_action() luego de ejecutar la accion real -este servicio nunca
    decide si la accion procede, solo la deja registrada.
    """

    @staticmethod
    def log_action(
        staff: PlatformStaff,
        action: str,
        entity: str,
        entity_id: int,
        details: str | dict | None = None,
    ) -> PlatformAuditLog:
        if isinstance(details, dict):
            details = json.dumps(details, default=str)
        return PlatformAuditLog.objects.create(
            platform_staff=staff,
            action=action,
            entity=entity,
            entity_id=entity_id,
            details=details or "",
        )


class PlatformDashboardService:
    """Agrega el resumen del panel interno (Especificacion de API §4.13)
    sobre las tablas ya existentes de core -no crea tablas nuevas, solo
    calcula sobre Tenant/Subscription/SubscriptionPayment.
    """

    _RECENT_LIMIT = 5

    @staticmethod
    def get_summary() -> dict:
        tenants_by_status = dict(
            Tenant.objects.values_list("status").annotate(count=Count("id"))
        )

        mrr = 0
        active_subscriptions = Subscription.objects.filter(
            status="active"
        ).select_related(None)
        for sub in active_subscriptions.only("billing_cycle", "price_paid"):
            months = {"MONTHLY": 1, "SEMIANNUAL": 6, "ANNUAL": 12}[sub.billing_cycle]
            mrr += sub.price_paid / months

        pending_payments_count = SubscriptionPayment.objects.filter(
            status="PENDING"
        ).count()

        recent_tenants = list(
            Tenant.objects.order_by("-created_on").values(
                "id", "company_name", "status", "created_on"
            )[: PlatformDashboardService._RECENT_LIMIT]
        )
        recently_suspended = list(
            Tenant.objects.filter(status="suspended")
            .order_by("-suspended_at")
            .values("id", "company_name", "suspended_at")[
                : PlatformDashboardService._RECENT_LIMIT
            ]
        )

        return {
            "tenants_by_status": tenants_by_status,
            "mrr": mrr,
            "pending_payments_count": pending_payments_count,
            "recent_tenants": recent_tenants,
            "recently_suspended": recently_suspended,
        }
