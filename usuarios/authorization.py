"""Autorizacion de supervisor (Bloque C) e intentos de acceso con correos
desconocidos (C.4).

El cajero pide una operacion sensible (anular, devolver, descuento sobre su
tope); si no tiene el permiso, un supervisor escribe su correo y contraseña
en el mismo equipo y recibe un token de un solo uso, de vida corta, atado al
permiso, al cajero que lo pidio y a la venta (o al porcentaje de descuento)
que vio al autorizar. El token viaja en la cabecera X-Supervisor-Authorization
y se consume dentro de la transaccion de la operacion.
"""

import hashlib
import secrets
from datetime import timedelta
from decimal import Decimal
from functools import cache
from ipaddress import ip_address

from django.contrib.auth.hashers import check_password, make_password
from django.utils import timezone
from rest_framework.exceptions import APIException

from usuarios.models import LoginAttempt, SupervisorAuthorization, User
from usuarios.services import AuditLogService, PermissionService

AUTHORIZATION_HEADER = "HTTP_X_SUPERVISOR_AUTHORIZATION"
AUTHORIZATION_TTL = timedelta(minutes=2)
# Operaciones que un supervisor puede autorizar desde el equipo del cajero.
AUTHORIZABLE_PERMISSIONS = frozenset({"SALES_VOID", "SALES_RETURN", "SALES_DISCOUNT"})
# Las que se atan a una venta concreta: autorizar "anular la V-000123" no
# sirve para anular otra.
_TARGETED_PERMISSIONS = frozenset({"SALES_VOID", "SALES_RETURN"})

_OPERATION_LABELS = {
    "SALES_VOID": "anular esta venta",
    "SALES_RETURN": "devolver esta venta",
    "SALES_DISCOUNT": "aplicar este descuento",
}


def authorization_token_from(request) -> str | None:
    token = request.META.get(AUTHORIZATION_HEADER, "").strip()
    return token or None


def _hash(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode()).hexdigest()


class SupervisorAuthorizationRequiredError(APIException):
    """403 que el frontend reconoce para abrir el modal de autorizacion y
    reintentar la operacion con el token (C.3)."""

    status_code = 403
    default_code = "SUPERVISOR_AUTHORIZATION_REQUIRED"

    def __init__(self, permission: str, **extra):
        super().__init__(
            {
                "error": {
                    "code": "SUPERVISOR_AUTHORIZATION_REQUIRED",
                    "message": (
                        f"Necesitas la autorización de un supervisor para "
                        f"{_OPERATION_LABELS.get(permission, 'esta operación')}."
                    ),
                    "permission": permission,
                    **extra,
                }
            }
        )


class SupervisorAuthorizationInvalidError(APIException):
    status_code = 403
    default_code = "SUPERVISOR_AUTHORIZATION_INVALID"
    default_detail = {
        "error": {
            "code": "SUPERVISOR_AUTHORIZATION_INVALID",
            "message": "La autorización no es válida o ya venció. Pídela de nuevo.",
        }
    }


class SupervisorAuthorizationDeniedError(APIException):
    """400 y no 403: el cajero si puede pedir autorizaciones; lo que fallo
    son los datos del supervisor. Asi el frontend no lo confunde con el 403
    que abre el modal."""

    status_code = 400
    default_code = "SUPERVISOR_AUTHORIZATION_DENIED"

    def __init__(self, code: str, message: str):
        super().__init__({"error": {"code": code, "message": message}})


def _client_ident(request) -> dict:
    # Misma IP que la bitacora de logins: get_ident() respeta NUM_PROXIES.
    from rest_framework.throttling import BaseThrottle

    return {
        "ip": BaseThrottle().get_ident(request),
        "user_agent": request.META.get("HTTP_USER_AGENT", "")[:200],
    }


class LoginAttemptService:
    RETENTION = timedelta(days=30)
    # Techo de filas por dia y negocio: por encima ya se sabe que hay un
    # ataque y seguir escribiendo solo llena el disco.
    DAILY_CAP = 5000

    @staticmethod
    def record_unknown_email(*, email: str, request, source: str) -> None:
        now = timezone.now()
        day_start = timezone.localtime(now).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        if (
            LoginAttempt.objects.filter(created_at__gte=day_start).count()
            >= LoginAttemptService.DAILY_CAP
        ):
            return
        ident = _client_ident(request)
        ip = ident["ip"]
        # get_ident() puede devolver un valor que no es una IP (cabecera
        # rara detras de un proxy mal configurado): se guarda vacio antes
        # que romper el login.
        try:
            ip_address(ip)
        except (TypeError, ValueError):
            ip = None
        LoginAttempt.objects.create(
            email=str(email)[:254],
            ip=ip,
            user_agent=ident["user_agent"],
            source=source,
        )

    @staticmethod
    def purge_expired() -> int:
        cutoff = timezone.now() - LoginAttemptService.RETENTION
        deleted, _ = LoginAttempt.objects.filter(created_at__lt=cutoff).delete()
        return deleted


# Hash fijo para comparar contra algo cuando el correo no existe: sin esto,
# "correo inexistente" responde mucho mas rapido que "contraseña incorrecta"
# y el tiempo de respuesta delata que correos son de supervisores.
# Se calcula una vez y tarde: al importar el modulo costaria un hash en
# cada arranque del proceso.
@cache
def _dummy_password_hash() -> str:
    return make_password(secrets.token_urlsafe(16))


def equalize_unknown_user_timing(password: str) -> None:
    check_password(password, _dummy_password_hash())


class SupervisorAuthorizationService:
    @staticmethod
    def grant(
        *,
        requested_by: User,
        email: str,
        password: str,
        permission: str,
        request,
        target_id: int | None = None,
        discount_percent: Decimal | None = None,
    ) -> tuple[str, SupervisorAuthorization]:
        supervisor = User.objects.filter(email=email, is_active=True).first()
        if supervisor is None:
            equalize_unknown_user_timing(password)
            LoginAttemptService.record_unknown_email(
                email=email,
                request=request,
                source=LoginAttempt.SOURCE_SUPERVISOR_AUTHORIZATION,
            )
            raise SupervisorAuthorizationDeniedError(
                "INVALID_CREDENTIALS", "Correo o contraseña incorrectos."
            )

        def deny(reason: str, code: str, message: str):
            # Queda a nombre del cajero: es quien estaba frente al equipo.
            AuditLogService.log_action(
                user=requested_by,
                action="SUPERVISOR_AUTHORIZATION_FAILED",
                entity="User",
                entity_id=supervisor.id,
                details={
                    "permission": permission,
                    "target_id": target_id,
                    "reason": reason,
                    **_client_ident(request),
                },
            )
            raise SupervisorAuthorizationDeniedError(code, message)

        if not supervisor.check_password(password):
            deny(
                "wrong_password",
                "INVALID_CREDENTIALS",
                "Correo o contraseña incorrectos.",
            )
        if supervisor.id == requested_by.id:
            deny(
                "self_authorization",
                "SELF_AUTHORIZATION_NOT_ALLOWED",
                "La autorización tiene que darla otra persona.",
            )
        if not PermissionService.check_permission(supervisor, permission):
            deny(
                "missing_permission",
                "SUPERVISOR_LACKS_PERMISSION",
                "Esa persona no tiene permiso para autorizar esta operación.",
            )

        raw_token = secrets.token_urlsafe(32)
        authorization = SupervisorAuthorization.objects.create(
            token_hash=_hash(raw_token),
            permission=permission,
            requested_by=requested_by,
            authorized_by=supervisor,
            target_id=target_id if permission in _TARGETED_PERMISSIONS else None,
            discount_percent=(
                discount_percent if permission == "SALES_DISCOUNT" else None
            ),
            expires_at=timezone.now() + AUTHORIZATION_TTL,
        )
        AuditLogService.log_action(
            user=supervisor,
            action="SUPERVISOR_AUTHORIZATION_GRANTED",
            entity="SupervisorAuthorization",
            entity_id=authorization.id,
            details={
                "permission": permission,
                "requested_by": requested_by.id,
                "target_id": authorization.target_id,
                "discount_percent": authorization.discount_percent,
            },
        )
        return raw_token, authorization

    @staticmethod
    def consume(
        *,
        raw_token: str | None,
        user: User,
        permission: str,
        target_id: int | None = None,
        discount_percent: Decimal | None = None,
    ) -> SupervisorAuthorization:
        """Gasta la autorizacion. Debe llamarse dentro de la transaccion de
        la operacion: si la operacion falla, el rollback la deja disponible
        para reintentar dentro de su vida util."""
        if not raw_token:
            extra = {}
            if discount_percent is not None:
                extra["requested_discount_percent"] = str(discount_percent)
            raise SupervisorAuthorizationRequiredError(permission, **extra)

        authorization = (
            SupervisorAuthorization.objects.select_for_update()
            .filter(token_hash=_hash(raw_token))
            .first()
        )
        if (
            authorization is None
            or authorization.used_at is not None
            or authorization.expires_at <= timezone.now()
            or authorization.requested_by_id != user.id
            or authorization.permission != permission
        ):
            raise SupervisorAuthorizationInvalidError()
        if permission in _TARGETED_PERMISSIONS and authorization.target_id != target_id:
            raise SupervisorAuthorizationInvalidError()
        if permission == "SALES_DISCOUNT" and (
            discount_percent is None
            or authorization.discount_percent is None
            or discount_percent > authorization.discount_percent
        ):
            raise SupervisorAuthorizationInvalidError()

        authorization.used_at = timezone.now()
        authorization.save(update_fields=["used_at"])
        return authorization

    @staticmethod
    def require(
        *,
        user: User,
        permission: str,
        raw_token: str | None,
        target_id: int | None = None,
    ) -> SupervisorAuthorization | None:
        """Permiso propio o autorizacion: devuelve None si el usuario puede
        por si mismo, o la autorizacion consumida si no."""
        if PermissionService.check_permission(user, permission):
            return None
        return SupervisorAuthorizationService.consume(
            raw_token=raw_token, user=user, permission=permission, target_id=target_id
        )

    @staticmethod
    def audit_details(authorization: SupervisorAuthorization | None) -> dict:
        """Lo que se suma al registro de la operacion autorizada, para que la
        bitacora muestre quien la pidio (el usuario del registro) y quien la
        autorizo. Solo ids: el correo lo resuelve AuditLogSerializer al
        leer, para que anonimizar a alguien no deje su correo vivo en el
        historial (Bloque B, decision 4)."""
        if authorization is None:
            return {}
        return {
            "authorized_by": authorization.authorized_by_id,
            "authorization_id": authorization.id,
        }
