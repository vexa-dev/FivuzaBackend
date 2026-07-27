import json

from django.core.cache import cache

from usuarios.models import (
    AuditLog,
    Permission,
    Role,
    RolePermission,
    RolePermissionsHistory,
    UserPermission,
)

_PERMISSION_CACHE_TTL = 300
_PERMISSION_CACHE_PREFIX = "usuarios:permissions"


class PermissionService:
    """Unico punto de verdad para saber que puede hacer un usuario -combina
    el permiso heredado del rol con los overrides individuales de
    UserPermission (Esquema Backend §4.2). HasPermission (permissions.py) es
    la unica clase de permisos de DRF que debe consultar este servicio.
    """

    @staticmethod
    def _cache_key(user_id: int) -> str:
        return f"{_PERMISSION_CACHE_PREFIX}:{user_id}"

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
        return AuditLog.objects.create(
            user=user,
            action=action,
            entity=entity,
            entity_id=entity_id,
            details=details or "",
        )
