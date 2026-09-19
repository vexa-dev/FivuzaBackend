import json
from datetime import timedelta

from django.db.models import Count

from core.models import (
    PlatformAuditLog,
    PlatformStaff,
    Subscription,
    SubscriptionPayment,
    Tenant,
)
from core.services.tenants import DATA_RETENTION_GRACE_DAYS


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

    Sprint 32: el tenant de demostracion comercial (Tenant.is_demo=True)
    se excluye de TODOS los agregados de aqui -sin esto, sus datos
    ficticios inflarian el conteo de tenants, el MRR y los pagos
    pendientes del equipo, dandole al negocio una foto falsa de si mismo."""

    _RECENT_LIMIT = 5

    @staticmethod
    def get_summary() -> dict:
        real_tenants = Tenant.objects.exclude(is_demo=True)

        tenants_by_status = dict(
            real_tenants.values_list("status").annotate(count=Count("id"))
        )

        mrr = 0
        active_subscriptions = Subscription.objects.filter(
            status="active", tenant__is_demo=False
        ).select_related(None)
        for sub in active_subscriptions.only("billing_cycle", "price_paid"):
            months = {"MONTHLY": 1, "SEMIANNUAL": 6, "ANNUAL": 12}[sub.billing_cycle]
            mrr += sub.price_paid / months

        pending_payments_count = SubscriptionPayment.objects.filter(
            status="PENDING", subscription__tenant__is_demo=False
        ).count()

        recent_tenants = list(
            real_tenants.order_by("-created_on").values(
                "id", "company_name", "status", "created_on"
            )[: PlatformDashboardService._RECENT_LIMIT]
        )
        recently_suspended = list(
            real_tenants.filter(status="suspended")
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
            for row in real_tenants.filter(status="canceled")
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
