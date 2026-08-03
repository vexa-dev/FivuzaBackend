import json
from datetime import timedelta

from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.utils import timezone

from usuarios.models import (
    AuditLog,
    PasswordResetToken,
    Permission,
    Role,
    RolePermission,
    RolePermissionsHistory,
    User,
    UserPermission,
)

_PERMISSION_CACHE_TTL = 300
_PERMISSION_CACHE_PREFIX = "usuarios:permissions"
_RESET_TOKEN_TTL_MINUTES = 30


class PermissionService:
    """Unico punto de verdad para saber que puede hacer un usuario -combina
    el permiso heredado del rol con los overrides individuales de
    UserPermission (Esquema Backend §4.2). HasPermission (permissions.py) es
    la unica clase de permisos de DRF que debe consultar este servicio.
    """

    @staticmethod
    def _cache_key(user_id: int) -> str:
        # usuarios.User.id NO es globalmente unico -cada esquema de tenant
        # tiene su propia secuencia autoincremental, asi que el usuario #1
        # existe en practicamente todos los tenants. Sin el schema_name en
        # la key, el cache de permisos de un tenant se filtraba al usuario
        # con el mismo id de OTRO tenant (encontrado via las pruebas de
        # impersonacion del Sprint 10, que crean muchos tenants nuevos -cada
        # uno con su propio usuario #1- en la misma corrida).
        return f"{_PERMISSION_CACHE_PREFIX}:{connection.schema_name}:{user_id}"

    @staticmethod
    def _resolve_codes(user) -> set[str]:
        role_codes = set(
            Permission.objects.filter(
                role_permissions__role_id=user.role_id
            ).values_list("code", flat=True)
        )
        overrides = UserPermission.objects.filter(user=user).select_related(
            "permission"
        )
        for override in overrides:
            if override.is_granted:
                role_codes.add(override.permission.code)
            else:
                role_codes.discard(override.permission.code)
        return role_codes

    @staticmethod
    def get_permission_codes(user) -> set[str]:
        key = PermissionService._cache_key(user.id)
        cached = cache.get(key)
        if cached is not None:
            return cached
        codes = PermissionService._resolve_codes(user)
        cache.set(key, codes, _PERMISSION_CACHE_TTL)
        return codes

    @staticmethod
    def check_permission(user, permission_code: str) -> bool:
        return permission_code in PermissionService.get_permission_codes(user)

    @staticmethod
    def invalidate_user_cache(user_id: int) -> None:
        cache.delete(PermissionService._cache_key(user_id))

    @staticmethod
    def invalidate_role_cache(role_id: int) -> None:
        """Invalida a TODOS los usuarios de un rol -se usa cuando cambia el
        conjunto de permisos del rol, no de un usuario individual."""
        from usuarios.models import User

        user_ids = User.objects.filter(role_id=role_id).values_list("id", flat=True)
        for user_id in user_ids:
            PermissionService.invalidate_user_cache(user_id)


class RoleService:
    """Gestiona la asignacion de permisos a un rol, dejando siempre un
    registro en RolePermissionsHistory -nunca se edita role_permissions
    directamente sin pasar por aqui (Esquema Backend §4.2)."""

    @staticmethod
    def grant_permission(
        role: Role, permission: Permission, changed_by
    ) -> RolePermission:
        role_permission, created = RolePermission.objects.get_or_create(
            role=role, permission=permission
        )
        if created:
            RolePermissionsHistory.objects.create(
                role=role,
                permission=permission,
                action="GRANTED",
                changed_by=changed_by,
            )
            PermissionService.invalidate_role_cache(role.id)
        return role_permission

    @staticmethod
    def revoke_permission(role: Role, permission: Permission, changed_by) -> None:
        deleted, _ = RolePermission.objects.filter(
            role=role, permission=permission
        ).delete()
        if deleted:
            RolePermissionsHistory.objects.create(
                role=role,
                permission=permission,
                action="REVOKED",
                changed_by=changed_by,
            )
            PermissionService.invalidate_role_cache(role.id)


class AuditLogService:
    """Unico punto de entrada para escribir en tenant.audit_logs (Esquema
    Backend §4.2, §4.3). Ninguna app de negocio inserta en AuditLog
    directamente -cada servicio que ejecuta una accion critica llama a
    log_action() explicitamente, nunca via señal generica."""

    @staticmethod
    def log_action(
        user,
        action: str,
        entity: str,
        entity_id: int,
        details: str | dict | None = None,
    ) -> AuditLog:
        if isinstance(details, dict):
            details = json.dumps(details, default=str)

        # Sprint 10 (Especificacion de API §4.24): si esta accion ocurrio
        # bajo una sesion de impersonacion, se marca explicitamente aqui -sin
        # que cada call site (repartido en las 4 apps de negocio) tenga que
        # saber nada de impersonacion. TenantValidatedJWTAuthentication fija
        # este contexto por request.
        from core.impersonation_context import get_impersonating_staff

        staff_id = get_impersonating_staff()
        if staff_id is not None:
            marker = f"[accion de soporte Fivuza, platform_staff #{staff_id}] "
            details = marker + (details or "")

        return AuditLog.objects.create(
            user=user,
            action=action,
            entity=entity,
            entity_id=entity_id,
            details=details or "",
        )


class PasswordResetService:
    """Flujo de 'olvide mi contraseña' (Sprint 5, hueco #1). request_reset()
    nunca revela si el correo existe -siempre se comporta igual desde
    afuera, para no filtrar que correos estan registrados."""

    @staticmethod
    def request_reset(*, email: str, schema_name: str, frontend_origin: str) -> None:
        try:
            user = User.objects.get(email=email, is_active=True)
        except User.DoesNotExist:
            return

        token = PasswordResetToken.objects.create(
            user=user,
            expires_at=timezone.now() + timedelta(minutes=_RESET_TOKEN_TTL_MINUTES),
        )

        from usuarios.tasks import send_password_reset_email

        send_password_reset_email.delay(
            user_id=user.id,
            token=token.token,
            schema_name=schema_name,
            frontend_origin=frontend_origin,
        )

    @staticmethod
    @transaction.atomic
    def confirm_reset(*, token: str, new_password: str) -> User:
        try:
            reset_token = PasswordResetToken.objects.select_for_update().get(
                token=token
            )
        except PasswordResetToken.DoesNotExist:
            raise ValidationError("El enlace de recuperacion es invalido o ya expiro.")

        if reset_token.used_at is not None or reset_token.expires_at < timezone.now():
            raise ValidationError("El enlace de recuperacion es invalido o ya expiro.")

        user = reset_token.user
        user.set_password(new_password)
        user.save(update_fields=["password"])

        reset_token.used_at = timezone.now()
        reset_token.save(update_fields=["used_at"])
        return user
