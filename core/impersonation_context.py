"""Contexto de impersonacion activo para el request en curso (Sprint 10).

TenantValidatedJWTAuthentication.authenticate() fija este valor en cada
request de tenant.users (a partir del claim impersonated_by_staff_id, o None
si el token es una sesion normal). AuditLogService.log_action() lo consulta
para marcar cada entrada de tenant.audit_logs escrita durante una sesion de
soporte, sin tener que tocar cada call site de las 4 apps de negocio
(Especificacion de API §4.24: "cada accion... marcada como accion de soporte
Fivuza").

Nota: se resetea explicitamente en cada authenticate() (tanto al entrar en
impersonacion como al salir), no solo cuando hay impersonacion -evita que un
valor quede pegado de un request anterior atendido por el mismo worker/hilo
en un request posterior sin token (caso teorico, sin impacto practico porque
ningun endpoint anonimo llama a log_action())."""

from contextvars import ContextVar

_impersonating_staff_id: ContextVar[int | None] = ContextVar(
    "impersonating_staff_id", default=None
)


def set_impersonating_staff(staff_id: int | None) -> None:
    _impersonating_staff_id.set(staff_id)


def get_impersonating_staff() -> int | None:
    return _impersonating_staff_id.get()
