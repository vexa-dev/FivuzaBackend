"""Tareas de Celery propias de inventario.

Incluye la creación mensual de la siguiente partición de inventory_movements
y audit_logs, y la alerta periódica de variantes por debajo de su min_stock.
"""

import logging
from datetime import date, timedelta

from celery import shared_task
from django_tenants.utils import get_tenant_model, schema_context

from core.partitioning import ensure_monthly_partition

logger = logging.getLogger(__name__)


@shared_task
def create_next_month_partitions() -> None:
    """Celery Beat mensual (Esquema Backend §9): crea la partición del mes
    siguiente para inventory_movements y audit_logs en todos los tenants,
    para que el 1ro de cada mes ya exista una partición donde escribir.
    Se corre a fin de mes, con margen, no el mismo día 1 (TRD §5.4)."""
    next_month = (date.today().replace(day=28) + timedelta(days=4)).replace(day=1)

    tenant_model = get_tenant_model()
    for tenant in tenant_model.objects.exclude(schema_name="public"):
        with schema_context(tenant.schema_name):
            ensure_monthly_partition(
                "inventory_movements", next_month.year, next_month.month
            )
            ensure_monthly_partition("audit_logs", next_month.year, next_month.month)


@shared_task
def alert_low_stock_variants() -> None:
    """Celery Beat periódica: revisa variantes por debajo de min_stock y
    encola la notificación (TRD §5.4). El canal real de notificación
    (email/push) todavía no existe en el proyecto -por ahora deja
    constancia en el log de Celery; el día que exista un NotificationService
    real, este task cambia el log de abajo por esa llamada."""
    from inventario.selectors import get_low_stock_variants

    tenant_model = get_tenant_model()
    for tenant in tenant_model.objects.exclude(schema_name="public"):
        with schema_context(tenant.schema_name):
            low_stock_count = get_low_stock_variants().count()
            if low_stock_count:
                logger.info(
                    "Stock bajo en %s: %s variante(s) por debajo de su mínimo.",
                    tenant.schema_name,
                    low_stock_count,
                )
