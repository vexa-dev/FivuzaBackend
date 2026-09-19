"""Ejecucion de tareas periodicas de Celery sobre todos los tenants.

Antes cada tarea iteraba los tenants con su propio for + schema_context, sin
manejo de errores: si un tenant fallaba (esquema a medio purgar, dato
corrupto), la excepcion cortaba el loop y el resto de negocios se quedaba
sin, por ejemplo, la particion del mes siguiente. Aqui cada tenant corre
aislado y los fallos quedan en el log (y en Sentry via logging).
"""

import logging
from collections.abc import Callable

from django_tenants.utils import (
    get_public_schema_name,
    get_tenant_model,
    schema_context,
)

logger = logging.getLogger(__name__)


def run_per_tenant(task_name: str, fn: Callable) -> list[str]:
    """Ejecuta fn(tenant) dentro del esquema de cada tenant (excepto public).
    Devuelve los schema_name que fallaron."""
    failed: list[str] = []
    tenants = get_tenant_model().objects.exclude(schema_name=get_public_schema_name())
    for tenant in tenants:
        try:
            with schema_context(tenant.schema_name):
                fn(tenant)
        except Exception:
            logger.exception("%s fallo en el tenant %s.", task_name, tenant.schema_name)
            failed.append(tenant.schema_name)
    return failed
