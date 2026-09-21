"""Filtros por fecha de los listados y reportes.

Dos problemas que tenia el patron anterior (`campo__date__gte=valor_crudo`):

1. Un valor mal escrito (`?date_from=hoy`) llegaba al ORM y reventaba con
   un 500 en vez de responder 400.
2. `campo__date` envuelve la columna en una funcion (el dia en hora de
   Lima), y PostgreSQL ya no puede usar el indice ni descartar particiones
   (audit_logs e inventory_movements estan particionadas por mes): cada
   filtro por fecha recorria la tabla entera.

Aqui las fechas se validan y se convierten en un rango de instantes
[inicio del primer dia, inicio del dia siguiente al ultimo) en la zona del
negocio. Mismo resultado que `__date`, pero comparando la columna tal cual.
"""

from datetime import date, datetime, time, timedelta

from django.utils import timezone
from rest_framework.exceptions import ValidationError

_INVALID_DATE = "Fecha inválida: usa el formato AAAA-MM-DD."


def parse_date(value: str, param: str) -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError({param: _INVALID_DATE}) from exc


def optional_date(params, param: str) -> date | None:
    value = params.get(param)
    return parse_date(value, param) if value else None


def required_date_range(
    params, from_param: str = "date_from", to_param: str = "date_to"
) -> tuple[date, date]:
    raw_from, raw_to = params.get(from_param), params.get(to_param)
    if not raw_from or not raw_to:
        raise ValidationError(f"{from_param} y {to_param} son requeridos.")
    date_from = parse_date(raw_from, from_param)
    date_to = parse_date(raw_to, to_param)
    if date_from > date_to:
        raise ValidationError({from_param: f"Debe ser anterior o igual a {to_param}."})
    return date_from, date_to


def _start_of_day(day: date) -> datetime:
    return timezone.make_aware(datetime.combine(day, time.min))


def day_range(
    field: str, date_from: date | None = None, date_to: date | None = None
) -> dict:
    """Lookups para `.filter(**day_range("created_at", desde, hasta))`.
    Ambos extremos son dias completos en la zona horaria del negocio."""
    lookups = {}
    if date_from is not None:
        lookups[f"{field}__gte"] = _start_of_day(date_from)
    if date_to is not None:
        lookups[f"{field}__lt"] = _start_of_day(date_to + timedelta(days=1))
    return lookups
