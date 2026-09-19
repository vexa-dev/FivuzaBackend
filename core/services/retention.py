from datetime import timedelta

from django.utils import timezone

from core.models import (
    Tenant,
    TenantSettings,
)
from core.services.tenants import DATA_RETENTION_GRACE_DAYS


class TenantDataRetentionService:
    """Sostiene realmente la Ley N 29733 sobre la cancelacion de un tenant
    (Sprint 33): un tenant `canceled` conserva sus datos en modo de solo
    lectura durante DATA_RETENTION_GRACE_DAYS (30 dias); pasado ese plazo,
    purge_expired_tenants() elimina su esquema fisico de forma
    irreversible. Sin esto, el periodo de gracia que TenantNotCanceled ya
    aplicaba desde el Sprint 8 nunca terminaba de verdad -un tenant
    cancelado podia seguir leyendo sus datos para siempre."""

    @staticmethod
    def is_within_grace_period(tenant: Tenant) -> bool:
        if tenant.status != "canceled" or tenant.canceled_at is None:
            return True
        deadline = tenant.canceled_at + timedelta(days=DATA_RETENTION_GRACE_DAYS)
        return timezone.now() <= deadline

    @staticmethod
    def purge_expired_tenants() -> list[dict]:
        """Tarea periodica diaria (Celery Beat): elimina el esquema fisico
        de cada tenant `canceled` cuyo periodo de gracia ya vencio.
        force_drop=True hace que django-tenants ejecute un DROP SCHEMA
        CASCADE real -irreversible- antes de borrar la fila de Tenant.

        No escribe en PlatformAuditLog (esa bitacora exige un
        platform_staff real, y esta tarea no tiene un actor humano detras)
        -el llamador (la tarea de Celery) es responsable de dejar
        constancia via logging con la lista que devuelve este metodo."""
        purged = []
        for tenant in Tenant.objects.filter(status="canceled"):
            if TenantDataRetentionService.is_within_grace_period(tenant):
                continue

            purged.append(
                {
                    "id": tenant.id,
                    "company_name": tenant.company_name,
                    "schema_name": tenant.schema_name,
                    "canceled_at": str(tenant.canceled_at),
                }
            )
            # TenantSettings.tenant es PROTECT -sin borrarla primero,
            # tenant.delete() explota con ProtectedError antes de llegar
            # siquiera al DROP SCHEMA.
            TenantSettings.objects.filter(tenant=tenant).delete()
            tenant.delete(force_drop=True)
        return purged
