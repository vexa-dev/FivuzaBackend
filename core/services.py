import json
from datetime import timedelta

from django.db.models import Count
from django.utils import timezone
from django_tenants.utils import get_public_schema_name

from core.models import (
    Domain,
    Plan,
    PlanFeature,
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
    La creacion de almacen 'Principal' y caja por defecto se completa en
    Sprint 3-10, cuando esos modelos ya existan.
    """

    # Catalogo minimo de permisos que Sprint 2 necesita: solo los de la app
    # usuarios (RBAC + auditoria + RRHH), que es lo unico que ya tiene
    # endpoints reales. Cada sprint que agregue un modulo de negocio con
    # permisos propios (inventario, ventas, etc.) debe sumar sus codigos aqui
    # y a los 3 roles que corresponda -no se anticipan codigos de modulos que
    # todavia no existen (Convenciones: no disenar para requisitos hipoteticos).
    _BASE_PERMISSIONS = [
        ("USERS_MANAGE_ROLES", "USERS"),
        ("USERS_MANAGE", "USERS"),
        ("USERS_VIEW_AUDIT", "USERS"),
        ("HR_MANAGE", "HR"),
        ("INVENTORY_VIEW", "INVENTORY"),
        ("INVENTORY_MANAGE", "INVENTORY"),
    ]
    _ROLE_PERMISSIONS = {
        "admin": [
            "USERS_MANAGE_ROLES",
            "USERS_MANAGE",
            "USERS_VIEW_AUDIT",
            "HR_MANAGE",
            "INVENTORY_VIEW",
            "INVENTORY_MANAGE",
        ],
        "manager": [
            "USERS_MANAGE",
            "USERS_VIEW_AUDIT",
            "HR_MANAGE",
            "INVENTORY_VIEW",
            "INVENTORY_MANAGE",
        ],
        "seller": ["INVENTORY_VIEW"],
    }

    @staticmethod
    def provision(tenant: Tenant) -> TenantSettings:
        """Se dispara desde post_save de Tenant (core/signals.py). Solo crea
        TenantSettings -tabla de core, vive en el esquema public, no depende
        de que el esquema fisico del tenant ya exista.

        La creacion de roles por defecto NO puede hacerse aqui: post_save se
        dispara DENTRO de TenantMixin.save(), ANTES de que ese mismo metodo
        llame a create_schema() -el esquema del tenant todavia no existe en
        este punto. Por eso seed_default_roles() se dispara aparte, desde la
        señal post_schema_sync (ver core/signals.py), que django-tenants
        emite recien despues de crear y migrar el esquema fisico.
        """
        settings, _ = TenantSettings.objects.get_or_create(tenant=tenant)
        return settings

    @staticmethod
    def seed_default_roles(tenant: Tenant) -> None:
        """Crea los 3 roles por defecto con su set de permisos base dentro
        del esquema ya migrado del tenant. Nunca se ejecuta sobre el esquema
        public -ese esquema no tiene las tablas de usuarios (TENANT_APP),
        y ademas no es un negocio real sobre el cual tenga sentido sembrar
        roles."""
        if tenant.schema_name == get_public_schema_name():
            return

        from django_tenants.utils import schema_context

        with schema_context(tenant.schema_name):
            TenantProvisioningService._seed_default_roles()

    @staticmethod
    def _seed_default_roles() -> None:
        # Import perezoso: usuarios es TENANT_APP, core es SHARED_APP -esta
        # es la unica excepcion documentada a "nunca importar modelos de otra
        # app" (Esquema Backend §8.2), porque el aprovisionamiento ocurre una
        # sola vez, al nacer el tenant.
        from usuarios.models import Permission, Role, RolePermission

        permissions_by_code = {}
        for code, module in TenantProvisioningService._BASE_PERMISSIONS:
            permission, _ = Permission.objects.get_or_create(
                code=code, defaults={"module": module}
            )
            permissions_by_code[code] = permission

        for role_name, codes in TenantProvisioningService._ROLE_PERMISSIONS.items():
            role, _ = Role.objects.get_or_create(
                name=role_name, defaults={"is_system_default": True}
            )
            for code in codes:
                RolePermission.objects.get_or_create(
                    role=role, permission=permissions_by_code[code]
                )


class FeatureFlagService:
    """Bloquea el acceso a funcionalidad opcional segun dos capas
    independientes: TenantSettings (el interruptor que el propio negocio
    prende/apaga, ej. "activar variantes") y PlanFeature (el techo que le
    pone su plan de suscripcion). Ambas capas deben permitirlo -Esquema
    Backend §8.2. Se adelanta al Sprint 3 porque variants_enabled y
    multi_warehouse_enabled ya aplican a inventario desde este sprint."""

    _TENANT_SETTINGS_FIELDS = {
        "HAS_VARIANTS": "variants_enabled",
        "HAS_MULTI_WAREHOUSE": "multi_warehouse_enabled",
        "HAS_HR_MODULE": "hr_module_enabled",
        "HAS_CASH_MODULE": "cash_module_enabled",
    }

    @staticmethod
    def is_enabled(tenant: Tenant, feature_code: str) -> bool:
        settings_field = FeatureFlagService._TENANT_SETTINGS_FIELDS.get(feature_code)
        if settings_field:
            settings = TenantSettings.objects.filter(tenant=tenant).first()
            if settings is not None and not getattr(settings, settings_field):
                return False

        plan_feature = (
            PlanFeature.objects.filter(
                plan__subscriptions__tenant=tenant,
                plan__subscriptions__status="active",
                feature_code=feature_code,
            )
            .order_by("-id")
            .first()
        )
        if plan_feature is not None and not plan_feature.is_enabled:
            return False

        return True


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
