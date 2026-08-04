import json
from datetime import timedelta

from django.db.models import Count
from django.utils import timezone
from django_tenants.utils import get_public_schema_name
from rest_framework.exceptions import APIException

from core.models import (
    Domain,
    Plan,
    PlanFeature,
    PlatformAuditLog,
    PlatformStaff,
    Subscription,
    SubscriptionDiscount,
    SubscriptionPayment,
    Tenant,
    TenantFeatureOverride,
    TenantImpersonationSession,
    TenantNote,
    TenantSettings,
)
from core.permissions import (
    CannotReactivateCanceledTenantError,
    TenantAlreadyCanceledError,
)

# 60 minutos (Especificacion de API §4.24; Ficha de Producto §6): suficiente
# para una sesion de soporte tipica sin dejar una puerta abierta indefinida
# al negocio de un tenant.
IMPERSONATION_SESSION_MINUTES = 60

_BILLING_CYCLE_DAYS = {"MONTHLY": 30, "SEMIANNUAL": 182, "ANNUAL": 365}
_BILLING_CYCLE_PRICE_FIELD = {
    "MONTHLY": "price_monthly",
    "SEMIANNUAL": "price_semiannual",
    "ANNUAL": "price_annual",
}
# Ley N 29733 (Especificacion de API §4.12): periodo de gracia de solo
# lectura/exportacion tras cancelar un tenant, antes de que
# TenantDataRetentionService.purge_expired_tenants() (fuera del alcance de
# este sprint) elimine el esquema fisico de forma irreversible.
DATA_RETENTION_GRACE_DAYS = 30


class TenantRegistrationService:
    """Registro de un tenant nuevo (Especificacion de API §4.9).

    Crea Tenant + Domain + Subscription (snapshot del precio del plan segun
    billing_cycle). El esquema fisico lo crea django-tenants de forma
    sincrona dentro de Tenant.save() (auto_create_schema=True) -Postgres no
    permite crear un esquema en un worker aparte sin duplicar la conexion de
    esta misma transaccion, asi que ese paso no se movio a Celery. Lo que si
    se volvio asincrono (Sprint 8, deuda cerrada) es la siembra de roles por
    defecto: el signal post_schema_sync encola provision_tenant_async en vez
    de ejecutarla en el mismo request, con Tenant.provisioning_status
    (PENDING/IN_PROGRESS/COMPLETED/FAILED) como seguimiento de estado.
    """

    @staticmethod
    def register(
        *,
        company_name: str,
        schema_name: str,
        domain: str,
        plan_code: str,
        billing_cycle: str,
        ruc: str | None = None,
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
        if tenant.status == "canceled":
            raise CannotReactivateCanceledTenantError()
        tenant.status = "active"
        tenant.suspended_at = None
        tenant.save(update_fields=["status", "suspended_at"])
        return tenant

    @staticmethod
    def cancel_tenant(tenant: Tenant, reason: str | None = None) -> Tenant:
        """Salida definitiva del negocio (Especificacion de API §4.12) -a
        diferencia de suspend_tenant, esta transicion no tiene vuelta atras:
        reactivate_tenant() se niega a operar sobre un tenant ya canceled."""
        if tenant.status == "canceled":
            raise TenantAlreadyCanceledError(tenant.canceled_at)
        tenant.status = "canceled"
        tenant.canceled_at = timezone.now()
        tenant.save(update_fields=["status", "canceled_at"])
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
        ("PURCHASES_MANAGE", "PURCHASES"),
        ("CASH_MANAGE", "CASH"),
        ("SALES_MANAGE", "SALES"),
    ]
    _ROLE_PERMISSIONS = {
        "admin": [
            "USERS_MANAGE_ROLES",
            "USERS_MANAGE",
            "USERS_VIEW_AUDIT",
            "HR_MANAGE",
            "INVENTORY_VIEW",
            "INVENTORY_MANAGE",
            "PURCHASES_MANAGE",
            "CASH_MANAGE",
            "SALES_MANAGE",
        ],
        "manager": [
            "USERS_MANAGE",
            "USERS_VIEW_AUDIT",
            "HR_MANAGE",
            "INVENTORY_VIEW",
            "INVENTORY_MANAGE",
            "PURCHASES_MANAGE",
            "CASH_MANAGE",
            "SALES_MANAGE",
        ],
        # "seller" no recibe CASH_MANAGE todavia a proposito (ver nota
        # historica de Sprint 12), pero SI recibe SALES_MANAGE desde este
        # sprint: registrar/editar un cliente o buscarlo durante una venta es
        # el caso de uso central de un vendedor en un POS real -a diferencia
        # de abrir su propia caja, que sigue siendo una decision de negocio
        # pendiente. Las promociones (mismo permiso, sin split fino todavia)
        # tambien quedan editables por un seller; si el negocio quiere
        # restringir eso a admin/manager solamente, se puede separar en un
        # codigo propio (ej. PROMOTIONS_MANAGE) en un sprint futuro.
        "seller": ["INVENTORY_VIEW", "SALES_MANAGE"],
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

    @staticmethod
    def seed_default_resources(tenant: Tenant) -> None:
        """Crea el almacen 'Principal' y la caja 'Caja Principal' por
        defecto al aprovisionar un tenant (Sprint 12) -cierra el pendiente
        que TenantProvisioningService.provision() dejo explicitamente
        anotado desde el Sprint 1 ("cuando esos modelos ya existan").
        Idempotente via get_or_create, mismo criterio que seed_default_roles."""
        if tenant.schema_name == get_public_schema_name():
            return

        from django_tenants.utils import schema_context

        with schema_context(tenant.schema_name):
            from inventario.models import Warehouse
            from ventas.models import CashRegister

            warehouse, _ = Warehouse.objects.get_or_create(
                name="Principal", defaults={"is_active": True}
            )
            CashRegister.objects.get_or_create(
                warehouse=warehouse, name="Caja Principal", defaults={"is_active": True}
            )


class PaymentAlreadyConfirmedError(APIException):
    status_code = 409
    default_code = "PAYMENT_ALREADY_CONFIRMED"
    default_detail = {
        "error": {
            "code": "PAYMENT_ALREADY_CONFIRMED",
            "message": "Este pago ya fue confirmado.",
        }
    }


class SubscriptionPaymentService:
    """Confirmacion manual de un pago recibido por transferencia bancaria
    directa (Especificacion de API §4.10; TRD §6.3, sin pasarela de pago).
    Confirmar extiende la vigencia de la suscripcion desde su vencimiento
    actual (o desde ahora, si ya estaba vencida) segun su billing_cycle, y
    la reactiva si estaba past_due."""

    @staticmethod
    def confirm_payment(payment: SubscriptionPayment) -> SubscriptionPayment:
        if payment.status == "PAID":
            raise PaymentAlreadyConfirmedError()

        payment.status = "PAID"
        payment.paid_at = timezone.now()
        payment.save(update_fields=["status", "paid_at"])

        subscription = payment.subscription
        extension_days = _BILLING_CYCLE_DAYS[subscription.billing_cycle]
        base = max(subscription.expires_at, timezone.now())
        subscription.expires_at = base + timedelta(days=extension_days)
        if subscription.status == "past_due":
            subscription.status = "active"

        update_fields = ["expires_at", "status"]
        # Sprint 11: si el tenant tiene un descuento vigente, se refleja en
        # el precio real cobrado a partir de ESTA confirmacion (no cambia
        # pagos ya confirmados en el pasado).
        discount = SubscriptionDiscountService.get_active_discount(subscription)
        if discount is not None:
            subscription.price_paid = SubscriptionDiscountService.apply_discount(
                subscription, discount
            )
            update_fields.append("price_paid")

        subscription.save(update_fields=update_fields)

        return payment


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
        "HAS_PURCHASES_MODULE": "purchases_enabled",
    }

    @staticmethod
    def is_enabled(tenant: Tenant, feature_code: str) -> bool:
        # Sprint 10 (Especificacion de API §4.25): un override individual
        # tiene prioridad sobre todo lo demas -a diferencia de
        # TenantSettings/PlanFeature (que solo pueden apagar), un override
        # tambien puede PRENDER una caracteristica que el plan no incluye,
        # por eso corta la evaluacion aqui en vez de sumarse a las demas capas.
        override = TenantFeatureOverride.objects.filter(
            tenant=tenant, feature_code=feature_code
        ).first()
        if override is not None:
            return override.is_enabled

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
        recently_canceled = [
            {
                "id": row["id"],
                "company_name": row["company_name"],
                "canceled_at": row["canceled_at"],
                "data_retention_until": row["canceled_at"]
                + timedelta(days=DATA_RETENTION_GRACE_DAYS),
            }
            for row in Tenant.objects.filter(status="canceled")
            .order_by("-canceled_at")
            .values("id", "company_name", "canceled_at")[
                : PlatformDashboardService._RECENT_LIMIT
            ]
        ]

        return {
            "tenants_by_status": tenants_by_status,
            "mrr": mrr,
            "pending_payments_count": pending_payments_count,
            "recent_tenants": recent_tenants,
            "recently_suspended": recently_suspended,
            "recently_canceled": recently_canceled,
        }


class NoImpersonableAdminUserError(APIException):
    status_code = 400
    default_code = "NO_ADMIN_USER"
    default_detail = {
        "error": {
            "code": "NO_ADMIN_USER",
            "message": "Este tenant no tiene un usuario administrador activo para impersonar.",
        }
    }


class TenantImpersonationService:
    """Sesion de soporte tecnico (Especificacion de API §4.24; Ficha de
    Producto §6): platform_staff (SUPER_ADMIN/SUPPORT) actua temporalmente
    como el admin del propio tenant, sin pedirle su contraseña. Expira sola
    a los IMPERSONATION_SESSION_MINUTES, o antes si se termina explicitamente
    -TenantValidatedJWTAuthentication rechaza el token apenas ended_at deja
    de ser null, asi que "terminar de inmediato" es real, no solo cosmetico."""

    @staticmethod
    def start_impersonation(staff: PlatformStaff, tenant: Tenant, reason: str) -> dict:
        from rest_framework_simplejwt.settings import api_settings
        from rest_framework_simplejwt.tokens import AccessToken

        admin, permission_codes = TenantImpersonationService._find_admin_user(tenant)

        expires_at = timezone.now() + timedelta(minutes=IMPERSONATION_SESSION_MINUTES)
        session = TenantImpersonationSession.objects.create(
            tenant=tenant, platform_staff=staff, reason=reason, expires_at=expires_at
        )

        access = AccessToken()
        access[api_settings.USER_ID_CLAIM] = admin.id
        access["schema_name"] = tenant.schema_name
        # impersonated_by_staff_id: lo consume el frontend del ERP para
        # mostrar el banner persistente (API Spec §4.24). impersonation_
        # session_id: lo consume el backend para poder revocar el token
        # antes de su vencimiento natural (ver _validate_impersonation_claim).
        access["impersonated_by_staff_id"] = staff.id
        access["impersonation_session_id"] = session.id
        access.set_exp(lifetime=timedelta(minutes=IMPERSONATION_SESSION_MINUTES))

        PlatformAuditLogService.log_action(
            staff=staff,
            action="TENANT_IMPERSONATION_STARTED",
            entity="Tenant",
            entity_id=tenant.id,
            details={
                "reason": reason,
                "session_id": session.id,
                "impersonated_user": admin.email,
            },
        )
        TenantImpersonationService._log_tenant_side(
            tenant,
            admin.id,
            "SUPPORT_IMPERSONATION_STARTED",
            session,
            f"Sesion de soporte iniciada por {staff.email}. Motivo: {reason}",
        )

        return {
            "session_id": session.id,
            "access_token": str(access),
            "expires_at": expires_at,
            # No forma parte del contrato literal de la Especificacion de API
            # §4.24 (que solo documenta session_id/access_token/expires_at),
            # pero sin esto el frontend del ERP no tiene forma de saber los
            # permisos del admin impersonado para renderizar el panel
            # correctamente -mismo shape que el "user" del login normal.
            "user": {
                "id": admin.id,
                "email": admin.email,
                "role": admin.role.name,
                "permissions": sorted(permission_codes),
            },
        }

    @staticmethod
    def end_session(session: TenantImpersonationSession) -> None:
        session.ended_at = timezone.now()
        session.save(update_fields=["ended_at"])

        PlatformAuditLogService.log_action(
            staff=session.platform_staff,
            action="TENANT_IMPERSONATION_ENDED",
            entity="Tenant",
            entity_id=session.tenant_id,
            details={"session_id": session.id},
        )
        TenantImpersonationService._log_tenant_side(
            session.tenant,
            None,
            "SUPPORT_IMPERSONATION_ENDED",
            session,
            "Sesion de soporte finalizada.",
            fallback_admin_lookup=True,
        )

    @staticmethod
    def _find_admin_user(tenant: Tenant):
        from django_tenants.utils import schema_context

        with schema_context(tenant.schema_name):
            from usuarios.models import User
            from usuarios.services import PermissionService

            admin = (
                User.objects.select_related("role")
                .filter(role__name="admin", is_active=True)
                .order_by("id")
                .first()
            )
            if admin is None:
                raise NoImpersonableAdminUserError()
            # select_related ya cacheo admin.role en esta instancia -sin
            # esto, acceder a admin.role.name mas tarde (fuera de este
            # schema_context, de vuelta en el esquema public del request de
            # platform_staff) dispararia un nuevo SELECT contra una tabla
            # "roles" que no existe fuera del esquema del tenant.
            return admin, PermissionService.get_permission_codes(admin)

    @staticmethod
    def _log_tenant_side(
        tenant: Tenant,
        user_id: int | None,
        action: str,
        session: TenantImpersonationSession,
        details: str,
        fallback_admin_lookup: bool = False,
    ) -> None:
        from django_tenants.utils import schema_context

        with schema_context(tenant.schema_name):
            from usuarios.models import User
            from usuarios.services import AuditLogService

            if user_id is None and fallback_admin_lookup:
                user = (
                    User.objects.filter(role__name="admin", is_active=True)
                    .order_by("id")
                    .first()
                )
            else:
                user = User.objects.filter(id=user_id).first()
            if user is None:
                return
            AuditLogService.log_action(
                user=user,
                action=action,
                entity="TenantImpersonationSession",
                entity_id=session.id,
                details=details,
            )


class TenantFeatureOverrideService:
    """Activa/desactiva UNA caracteristica para UN tenant especifico sin
    tocar su plan contratado (Especificacion de API §4.25). Solo SUPER_ADMIN."""

    @staticmethod
    def set_override(
        tenant: Tenant, feature_code: str, is_enabled: bool
    ) -> TenantFeatureOverride:
        override, _ = TenantFeatureOverride.objects.update_or_create(
            tenant=tenant,
            feature_code=feature_code,
            defaults={"is_enabled": is_enabled},
        )
        return override

    @staticmethod
    def remove_override(tenant: Tenant, feature_code: str) -> None:
        TenantFeatureOverride.objects.filter(
            tenant=tenant, feature_code=feature_code
        ).delete()


class TenantNoteService:
    """Notas internas del equipo Fivuza sobre un tenant (Especificacion de
    API §4.25) -nunca visibles para el propio negocio."""

    @staticmethod
    def add_note(tenant: Tenant, staff: PlatformStaff, text: str) -> TenantNote:
        return TenantNote.objects.create(tenant=tenant, platform_staff=staff, text=text)


class InvalidDiscountError(APIException):
    status_code = 400
    default_code = "INVALID_DISCOUNT"
    default_detail = {
        "error": {
            "code": "INVALID_DISCOUNT",
            "message": "Especifique discount_percent o override_price, nunca ambos.",
        }
    }


class SubscriptionDiscountService:
    """Condicion especial de precio negociada con un tenant puntual
    (Especificacion de API §4.25) -se aplica recien en la SIGUIENTE
    confirmacion de pago (SubscriptionPaymentService.confirm_payment), nunca
    retroactivamente sobre pagos ya confirmados."""

    @staticmethod
    def create_discount(
        *,
        subscription: Subscription,
        discount_percent=None,
        override_price=None,
        reason: str,
        expires_at=None,
    ) -> SubscriptionDiscount:
        has_percent = discount_percent is not None
        has_override = override_price is not None
        if has_percent == has_override:  # ambos o ninguno
            raise InvalidDiscountError()
        return SubscriptionDiscount.objects.create(
            subscription=subscription,
            discount_percent=discount_percent,
            override_price=override_price,
            reason=reason,
            expires_at=expires_at,
        )

    @staticmethod
    def get_active_discount(subscription: Subscription) -> SubscriptionDiscount | None:
        from django.db.models import Q

        now = timezone.now()
        return (
            SubscriptionDiscount.objects.filter(subscription=subscription)
            .filter(Q(expires_at__isnull=True) | Q(expires_at__gt=now))
            .order_by("-created_at")
            .first()
        )

    @staticmethod
    def apply_discount(subscription: Subscription, discount: SubscriptionDiscount):
        from decimal import Decimal

        if discount.override_price is not None:
            return discount.override_price

        plan_price = getattr(
            subscription.plan, _BILLING_CYCLE_PRICE_FIELD[subscription.billing_cycle]
        )
        return plan_price * (Decimal("1") - discount.discount_percent / Decimal("100"))

    @staticmethod
    def remove_discount(discount: SubscriptionDiscount) -> None:
        discount.delete()


class TenantOnboardingService:
    """Checklist de onboarding computado, no almacenado (Especificacion de
    API §4.26) -siempre refleja el estado real del esquema del tenant en
    el momento de la consulta."""

    @staticmethod
    def get_checklist(tenant: Tenant) -> dict:
        empty = {
            "has_catalog": False,
            "has_first_sale": False,
            "has_users_created": False,
        }
        # El tenant "public" (esquema compartido) no es un negocio real -no
        # tiene las tablas de las 4 apps de negocio (son TENANT_APPS), asi
        # que consultarlas revienta con "relation does not exist" en vez de
        # simplemente no tener nada que mostrar.
        if tenant.schema_name == get_public_schema_name():
            return empty

        from django_tenants.utils import schema_context

        with schema_context(tenant.schema_name):
            from inventario.models import Product
            from usuarios.models import User
            from ventas.models import Sale

            return {
                "has_catalog": Product.objects.exists(),
                "has_first_sale": Sale.objects.filter(status="COMPLETED").exists(),
                "has_users_created": User.objects.exists(),
            }


class TenantConsumptionService:
    """Reporte de consumo por tenant (Especificacion de API §4.26): agrega
    datos ya existentes, no crea ninguna tabla nueva."""

    @staticmethod
    def get_report(tenant: Tenant) -> dict:
        empty = {
            "sales_count_last_30_days": 0,
            "active_users_count": 0,
            "catalog_size": 0,
        }
        # Ver nota identica en TenantOnboardingService.get_checklist -el
        # tenant "public" no tiene las tablas de negocio.
        if tenant.schema_name == get_public_schema_name():
            return empty

        from django_tenants.utils import schema_context

        with schema_context(tenant.schema_name):
            from inventario.models import Product
            from usuarios.models import User
            from ventas.models import Sale

            thirty_days_ago = timezone.now() - timedelta(days=30)
            return {
                "sales_count_last_30_days": Sale.objects.filter(
                    status="COMPLETED", created_at__gte=thirty_days_ago
                ).count(),
                "active_users_count": User.objects.filter(is_active=True).count(),
                "catalog_size": Product.objects.count(),
            }


class TenantHealthService:
    """Cruza errores recientes de Sentry (via su API REST, filtrando por el
    tag tenant=schema_name que SentryTenantTagMiddleware fija en cada
    request) con actividad propia del tenant (Especificacion de API §4.26).

    Si SENTRY_API_TOKEN/SENTRY_ORG_SLUG/SENTRY_PROJECT_SLUG no estan
    configurados (o la API de Sentry no responde), degrada a
    recent_errors_count=0 en vez de fallar -el panel de salud sigue siendo
    util con solo la actividad propia, y un problema de red hacia Sentry
    nunca debe tumbar este endpoint."""

    @staticmethod
    def get_health(tenant: Tenant) -> dict:
        sentry_data = TenantHealthService._fetch_sentry_errors(tenant.schema_name)
        activity = TenantHealthService._fetch_own_activity(tenant)
        return {**sentry_data, **activity}

    @staticmethod
    def _fetch_sentry_errors(schema_name: str) -> dict:
        from django.conf import settings

        empty = {"recent_errors_count": 0, "last_error_at": None}
        if not (
            settings.SENTRY_API_TOKEN
            and settings.SENTRY_ORG_SLUG
            and settings.SENTRY_PROJECT_SLUG
        ):
            return empty

        import json
        import urllib.error
        import urllib.parse
        import urllib.request

        base_url = (
            f"https://sentry.io/api/0/projects/{settings.SENTRY_ORG_SLUG}"
            f"/{settings.SENTRY_PROJECT_SLUG}/issues/"
        )
        query = urllib.parse.urlencode(
            {"query": f"tag:tenant:{schema_name}", "statsPeriod": "24h"}
        )
        request = urllib.request.Request(
            f"{base_url}?{query}",
            headers={"Authorization": f"Bearer {settings.SENTRY_API_TOKEN}"},
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                issues = json.loads(response.read())
        except (urllib.error.URLError, TimeoutError, ValueError):
            return empty

        last_seen_values = [
            issue["lastSeen"] for issue in issues if issue.get("lastSeen")
        ]
        return {
            "recent_errors_count": len(issues),
            "last_error_at": max(last_seen_values) if last_seen_values else None,
        }

    @staticmethod
    def _fetch_own_activity(tenant: Tenant) -> dict:
        empty = {"last_login_at": None, "last_sale_at": None}
        # Ver nota identica en TenantOnboardingService.get_checklist -el
        # tenant "public" no tiene las tablas de negocio.
        if tenant.schema_name == get_public_schema_name():
            return empty

        from django_tenants.utils import schema_context

        with schema_context(tenant.schema_name):
            from usuarios.models import User
            from ventas.models import Sale

            last_login = (
                User.objects.exclude(last_login__isnull=True)
                .order_by("-last_login")
                .first()
            )
            last_sale = (
                Sale.objects.filter(status="COMPLETED").order_by("-created_at").first()
            )
            return {
                "last_login_at": last_login.last_login if last_login else None,
                "last_sale_at": last_sale.created_at if last_sale else None,
            }
