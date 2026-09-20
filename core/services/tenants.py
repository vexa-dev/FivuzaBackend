from datetime import timedelta

from django.db import connection, transaction
from django.utils import timezone
from django_tenants.utils import get_public_schema_name

from core.models import (
    Domain,
    Plan,
    Subscription,
    Tenant,
    TenantSettings,
)
from core.permissions import (
    CannotReactivateCanceledTenantError,
    TenantAlreadyCanceledError,
    TermsNotAcceptedError,
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
        accept_terms: bool,
        ruc: str | None = None,
    ) -> Tenant:
        if not accept_terms:
            raise TermsNotAcceptedError()

        plan = Plan.objects.get(code=plan_code, is_active=True)

        from core.legal import TERMS_VERSION

        tenant = Tenant.objects.create(
            schema_name=schema_name,
            company_name=company_name,
            ruc=ruc,
            terms_accepted_at=timezone.now(),
            terms_version_accepted=TERMS_VERSION,
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
        # Bloque A.5: ver el catalogo y ver cuanto cuesta son dos cosas
        # distintas -el cajero necesita lo primero para vender y no debe
        # tener lo segundo.
        ("INVENTORY_VIEW_COST", "INVENTORY"),
        ("INVENTORY_MANAGE", "INVENTORY"),
        ("PURCHASES_MANAGE", "PURCHASES"),
        ("CASH_MANAGE", "CASH"),
        # Bloque A.1: abrir y cerrar se separan de CASH_MANAGE porque son
        # decisiones distintas del negocio ("mi cajero abre su caja pero no
        # la cierra"). CASH_MANAGE los sigue implicando (compatibilidad hacia
        # atras: ningun tenant en marcha pierde acceso) -ver
        # PermissionService._resolve_codes.
        ("CASH_OPEN", "CASH"),
        ("CASH_CLOSE", "CASH"),
        ("SALES_MANAGE", "SALES"),
        # Sprint 18: separados de SALES_MANAGE a proposito (Plan de
        # Implementacion, Sprint 18: "un cajero puede vender sin poder anular
        # lo ya cobrado"). SALES_RETURN si va a seller -procesar una
        # devolucion es una operacion de mostrador rutinaria, muy distinta de
        # anular una venta completa.
        ("SALES_VOID", "SALES"),
        ("SALES_RETURN", "SALES"),
        # Sprint 29: vertical de Gimnasios, un solo permiso para todo el
        # modulo (mismo criterio que HR_MANAGE, sin split fino).
        ("GYM_MANAGE", "GYM"),
        # Sprint 33 (Ley N 29733): separado a proposito de USERS_MANAGE -un
        # respaldo completo del negocio es mas sensible que administrar
        # usuarios, y solo admin lo recibe por defecto (ni siquiera manager).
        ("DATA_EXPORT", "COMPLIANCE"),
        # Bloque A.0: hasta ahora los interruptores operativos solo se tocaban
        # desde el panel interno de Fivuza (TenantSettingsViewSet, IsPlatformStaff).
        # Este permiso habilita el endpoint propio del tenant.
        ("SETTINGS_MANAGE", "SETTINGS"),
    ]
    _ROLE_PERMISSIONS = {
        "admin": [
            "USERS_MANAGE_ROLES",
            "USERS_MANAGE",
            "USERS_VIEW_AUDIT",
            "HR_MANAGE",
            "INVENTORY_VIEW",
            "INVENTORY_VIEW_COST",
            "INVENTORY_MANAGE",
            "PURCHASES_MANAGE",
            "CASH_MANAGE",
            "CASH_OPEN",
            "CASH_CLOSE",
            "SALES_MANAGE",
            "SALES_VOID",
            "SALES_RETURN",
            "GYM_MANAGE",
            "DATA_EXPORT",
            "SETTINGS_MANAGE",
        ],
        # SETTINGS_MANAGE queda fuera a proposito (Bloque A.0): decidir si el
        # cajero puede abrir o cerrar caja es una decision del dueño, no de
        # quien supervisa el turno -mismo criterio que DATA_EXPORT.
        "manager": [
            "USERS_MANAGE",
            "USERS_VIEW_AUDIT",
            "HR_MANAGE",
            "INVENTORY_VIEW",
            "INVENTORY_VIEW_COST",
            "INVENTORY_MANAGE",
            "PURCHASES_MANAGE",
            "CASH_MANAGE",
            "CASH_OPEN",
            "CASH_CLOSE",
            "SALES_MANAGE",
            "SALES_VOID",
            "SALES_RETURN",
            "GYM_MANAGE",
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
        # SALES_VOID queda fuera a proposito (Sprint 18): un cajero no anula
        # lo que el mismo cobro. SALES_RETURN si entra: procesar una
        # devolucion en el mostrador es parte normal de su turno.
        "seller": ["INVENTORY_VIEW", "SALES_MANAGE", "SALES_RETURN"],
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

        with schema_context(tenant.schema_name), transaction.atomic():
            TenantProvisioningService._lock_provisioning(tenant.schema_name)
            TenantProvisioningService._seed_default_roles()

    @staticmethod
    def _lock_provisioning(schema_name: str) -> None:
        """Serializa la siembra de un mismo tenant. La tarea de Celery
        (post_schema_sync) y otros llamadores (migraciones de datos,
        seed_e2e) pueden sembrar a la vez; como Role.name no es unico,
        dos get_or_create en paralelo duplicaban los roles por defecto.
        El lock vive hasta el fin de la transaccion."""
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))",
                [f"fivuza-provision:{schema_name}"],
            )

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

        with schema_context(tenant.schema_name), transaction.atomic():
            TenantProvisioningService._lock_provisioning(tenant.schema_name)
            from inventario.models import Warehouse
            from ventas.models import CashRegister

            warehouse, _ = Warehouse.objects.get_or_create(
                name="Principal", defaults={"is_active": True}
            )
            CashRegister.objects.get_or_create(
                warehouse=warehouse, name="Caja Principal", defaults={"is_active": True}
            )
