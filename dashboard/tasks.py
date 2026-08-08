"""Tareas de Celery propias de dashboard."""

import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task
def refresh_materialized_views() -> None:
    """Celery Beat periódica (cada 5 min, ver CELERY_BEAT_SCHEDULE): delega
    en DashboardRefreshService, que decide por tenant si ya le toca
    refrescar segun su dashboard_refresh_minutes configurado (Sprint 24,
    Esquema Backend §9.2). Nunca se invoca desde un request HTTP directo."""
    from dashboard.services import DashboardRefreshService

    refreshed = DashboardRefreshService.refresh_due_tenants()
    if refreshed:
        logger.info(
            "Vistas materializadas del dashboard refrescadas en %s tenant(s).",
            refreshed,
        )
