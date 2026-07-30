"""Utilidades de particionado nativo de Postgres (RANGE sobre created_at),
compartidas por cualquier tabla particionada del proyecto (`audit_logs`,
`inventory_movements`). Vive en `core` -y no en `usuarios`/`inventario`-
porque ambas apps la necesitan y ninguna de las dos es dueña del concepto.

Django no soporta particionado declarativo de forma nativa: el modelo
Django sigue siendo un ORM normal sobre la tabla padre, el particionado
es un detalle fisico que se gestiona por fuera con SQL crudo. Esto es
seguro porque Django solo compara el estado de las migraciones contra
models.py (nunca introspecciona la base de datos real) -ejecutar SQL
crudo aqui no genera drift para makemigrations.
"""

from datetime import date

from django.db import connection


def ensure_default_partition(table_name: str) -> None:
    """Particion 'catch-all' para filas fuera de cualquier rango mensual ya
    creado -sin esto, un INSERT con created_at fuera de rango (ej. la tarea
    mensual de creacion de particiones fallo o se atraso) es rechazado por
    Postgres en vez de aceptarse en algun lado."""
    with connection.cursor() as cursor:
        cursor.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {table_name}_default
            PARTITION OF {table_name} DEFAULT
            """  # noqa: S608
        )


def bootstrap_initial_partitions(table_name: str, months_ahead: int = 2) -> None:
    """Crea la particion del mes actual + `months_ahead` meses siguientes,
    ademas de la particion default. Se llama una vez desde la migracion que
    convierte la tabla en particionada, y el mismo calculo de meses lo
    reutiliza la tarea Celery Beat mensual (Esquema Backend §9) para no
    quedarse sin particion donde escribir al cambiar de mes."""
    today = date.today()
    year, month = today.year, today.month
    for _ in range(months_ahead + 1):
        ensure_monthly_partition(table_name, year, month)
        month += 1
        if month > 12:
            month = 1
            year += 1
    ensure_default_partition(table_name)


def ensure_monthly_partition(table_name: str, year: int, month: int) -> None:
    """Crea la particion de `table_name` para el mes (year, month) si no
    existe todavia. Nombre de partition: `{table_name}_YYYYMM`. Idempotente
    -se puede llamar de nuevo sobre un mes ya particionado sin error
    (CREATE TABLE IF NOT EXISTS)."""
    partition_name = f"{table_name}_{year:04d}{month:02d}"
    start = f"{year:04d}-{month:02d}-01"
    if month == 12:
        end = f"{year + 1:04d}-01-01"
    else:
        end = f"{year:04d}-{month + 1:02d}-01"

    with connection.cursor() as cursor:
        cursor.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {partition_name}
            PARTITION OF {table_name}
            FOR VALUES FROM (%s) TO (%s)
            """,  # noqa: S608 -- table_name/partition_name son constantes internas, nunca input de usuario
            [start, end],
        )
