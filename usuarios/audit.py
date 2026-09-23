"""Bitacora automatica de los CRUD del tenant (Bloque B.1).

Hasta el Bloque B solo quedaban en tenant.audit_logs las acciones con una
llamada explicita a AuditLogService.log_action() (venta, anulacion, cierre de
caja...). TenantAuditMixin cubre el resto: todo ViewSet de escritura de las
apps de negocio registra alta, edicion y baja, con los valores antes y
despues de cada campo que cambio.

Inspirado en core.views.AuditLoggedViewSetMixin (el de plataforma), con dos
diferencias: guarda QUE cambio, no solo que algo cambio, y la escritura y el
registro van en la misma transaccion -si el registro falla, el cambio no
queda hecho sin auditar.
"""

from contextlib import contextmanager
from datetime import date, datetime, time
from decimal import Decimal

from django.db import transaction

from usuarios.services import AuditLogService

# Nunca se escriben en la bitacora, ni siquiera enmascarados: secretos y
# campos tecnicos que cambian en cada guardado sin que nadie los edite.
_EXCLUDED_FIELDS = frozenset(
    {
        "password",
        "last_login",
        "search_vector",
        "created_at",
        "updated_at",
    }
)
_EXCLUDED_FIELD_MARKERS = ("token", "secret", "_hash")
# "pin" como palabra del nombre, no como subcadena: "shipping" no es un PIN.
_PIN_WORD = "pin"

# Los mismos campos que PersonalDataService.anonymize_user() borra: si
# quedaran en claro en la bitacora, anonimizar a una persona (Ley N 29733)
# dejaria sus datos vivos en el historial. Se registra que cambiaron, no a
# que valor.
PERSONAL_DATA_FIELDS = frozenset({"email", "full_name", "document_number", "phone"})
PERSONAL_DATA_MASK = "[dato personal]"

# Un registro no deberia pesar mas que esto aunque el modelo tenga campos de
# texto largos (notas, descripciones).
MAX_VALUE_LENGTH = 200


def _is_excluded(field_name: str) -> bool:
    if field_name in _EXCLUDED_FIELDS or _PIN_WORD in field_name.split("_"):
        return True
    return any(marker in field_name for marker in _EXCLUDED_FIELD_MARKERS)


def _to_json_value(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, str) and len(value) > MAX_VALUE_LENGTH:
        return value[:MAX_VALUE_LENGTH] + "…"
    return value


def snapshot(instance) -> dict:
    """Valores actuales de los campos propios del modelo (las FK como id),
    ya filtrados y listos para comparar. Los campos personales se guardan
    enmascarados desde aqui, asi que ningun camino los deja pasar."""
    values = {}
    for field in instance._meta.concrete_fields:
        if _is_excluded(field.name):
            continue
        value = getattr(instance, field.attname)
        if field.name in PERSONAL_DATA_FIELDS and value not in (None, ""):
            value = PERSONAL_DATA_MASK
        values[field.name] = value
    return values


def diff(before: dict, after: dict) -> dict:
    """{campo: {"before": x, "after": y}} solo de lo que cambio. No ve el
    cambio de un dato personal entre dos valores no vacios (ambos quedan
    enmascarados igual): perform_update lo resuelve aparte."""
    changes = {}
    for field, new_value in after.items():
        old_value = before.get(field)
        if old_value != new_value:
            changes[field] = {
                "before": _to_json_value(old_value),
                "after": _to_json_value(new_value),
            }
    return changes


def _raw_personal_values(instance) -> dict:
    return {
        field.name: getattr(instance, field.attname)
        for field in instance._meta.concrete_fields
        if field.name in PERSONAL_DATA_FIELDS
    }


def created_details(instance) -> dict:
    return {
        field: _to_json_value(value)
        for field, value in snapshot(instance).items()
        if value not in (None, "")
    }


class TenantAuditMixin:
    """Registra CREATE/UPDATE/DELETE de un ViewSet del tenant en
    tenant.audit_logs. Va primero en las bases de la clase:

        class CategoryViewSet(TenantAuditMixin, SoftDeleteDestroyMixin,
                              viewsets.ModelViewSet): ...

    Si la vista necesita su propio perform_*, llama a super() (el
    registro envuelve lo que venga debajo) o, si la baja no es un delete
    de DRF sino un servicio, usa self.audited_destroy(instance) como
    contexto -ver RoleViewSet.

    No se aplica a los ViewSets cuyo create() delega en un servicio que ya
    registra con mas contexto (venta, devolucion...): ahi no hay
    perform_create y el mixin no tendria nada que envolver."""

    def audit_entity_name(self, instance) -> str:
        return instance.__class__.__name__

    def _log(self, action: str, instance, entity_id, details: dict) -> None:
        AuditLogService.log_action(
            user=self.request.user,
            action=action,
            entity=self.audit_entity_name(instance),
            entity_id=entity_id,
            details=details,
        )

    def perform_create(self, serializer):
        with transaction.atomic():
            super().perform_create(serializer)
            instance = serializer.instance
            self._log("CREATE", instance, instance.pk, created_details(instance))

    def perform_update(self, serializer):
        instance = serializer.instance
        before = snapshot(instance)
        personal_before = _raw_personal_values(instance)
        with transaction.atomic():
            super().perform_update(serializer)
            instance = serializer.instance
            changes = diff(before, snapshot(instance))
            # Un cambio de un dato personal a otro valor no nulo queda
            # enmascarado igual en ambos lados, y diff() no lo veria.
            for field, old_value in personal_before.items():
                if field not in changes and getattr(instance, field) != old_value:
                    changes[field] = {
                        "before": PERSONAL_DATA_MASK,
                        "after": PERSONAL_DATA_MASK,
                    }
            # Un PATCH que no cambia nada no es una escritura que valga la
            # pena registrar.
            if changes:
                self._log("UPDATE", instance, instance.pk, changes)

    @contextmanager
    def audited_destroy(self, instance):
        entity_id = instance.pk
        details = created_details(instance)
        with transaction.atomic():
            yield
            self._log("DELETE", instance, entity_id, details)

    def perform_destroy(self, instance):
        with self.audited_destroy(instance):
            super().perform_destroy(instance)
