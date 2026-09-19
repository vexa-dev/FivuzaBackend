import json
from datetime import timedelta

from django.utils import timezone
from django_tenants.utils import get_public_schema_name

from core.models import (
    PlatformStaff,
    Tenant,
    TenantNote,
)


class TenantNoteService:
    """Notas internas del equipo Fivuza sobre un tenant (Especificacion de
    API §4.25) -nunca visibles para el propio negocio."""

    @staticmethod
    def add_note(tenant: Tenant, staff: PlatformStaff, text: str) -> TenantNote:
        return TenantNote.objects.create(tenant=tenant, platform_staff=staff, text=text)


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
