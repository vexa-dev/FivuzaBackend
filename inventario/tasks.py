"""Tareas de Celery propias de inventario.

Incluye la creación mensual de la siguiente partición de inventory_movements
y audit_logs, y la alerta periódica de variantes por debajo de su min_stock.
"""

import logging
from datetime import timedelta

from celery import shared_task
from django.utils import timezone

from core.partitioning import ensure_monthly_partition
from core.tenant_tasks import run_per_tenant

# Meses por adelantado: si Beat no corre un dia 28 (deploy, caida), el mes
# siguiente igual tiene particion creada desde el ciclo anterior.
_PARTITION_MONTHS_AHEAD = 2

logger = logging.getLogger(__name__)


@shared_task
def create_next_month_partitions() -> None:
    """Celery Beat mensual (Esquema Backend §9): crea la partición del mes
    siguiente para inventory_movements y audit_logs en todos los tenants,
    para que el 1ro de cada mes ya exista una partición donde escribir.
    Se corre a fin de mes, con margen, no el mismo día 1 (TRD §5.4)."""
    months = []
    month = timezone.localdate().replace(day=1)
    for _ in range(_PARTITION_MONTHS_AHEAD):
        month = (month.replace(day=28) + timedelta(days=4)).replace(day=1)
        months.append(month)

    def _run(tenant):
        for month in months:
            ensure_monthly_partition("inventory_movements", month.year, month.month)
            ensure_monthly_partition("audit_logs", month.year, month.month)

    run_per_tenant("create_next_month_partitions", _run)


@shared_task
def alert_low_stock_variants() -> None:
    """Celery Beat periódica: revisa variantes por debajo de min_stock y
    encola la notificación (TRD §5.4). El canal real de notificación
    (email/push) todavía no existe en el proyecto -por ahora deja
    constancia en el log de Celery; el día que exista un NotificationService
    real, este task cambia el log de abajo por esa llamada."""
    from inventario.selectors import get_low_stock_variants

    def _run(tenant):
        low_stock_count = get_low_stock_variants().count()
        if low_stock_count:
            logger.info(
                "Stock bajo en %s: %s variante(s) por debajo de su mínimo.",
                tenant.schema_name,
                low_stock_count,
            )

    run_per_tenant("alert_low_stock_variants", _run)
