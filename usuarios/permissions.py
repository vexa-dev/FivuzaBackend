"""Permisos DRF específicos de usuarios (RBAC/RRHH), además de los compartidos.

HasModulePermission vive aquí porque delega en PermissionService (services.py),
el único punto de verdad para combinar el permiso heredado del rol con los
overrides individuales de UserPermission.
"""

from rest_framework.permissions import BasePermission

from usuarios.services import PermissionService


def HasModulePermission(code):
    """Factory de permiso DRF: exige que el usuario autenticado (tenant.users)
    tenga el permiso `code`, consultando siempre PermissionService -nunca se
    recalcula el set de permisos fuera de ese servicio (Esquema Backend §4.2).
    Es la pieza que todas las apps de negocio usaran a partir del Sprint 3.

    Uso: permission_classes = [IsAuthenticated, HasModulePermission("USERS_MANAGE")]
    """

    class _HasModulePermission(BasePermission):
        def has_permission(self, request, view):
            user = request.user
            if not hasattr(user, "role_id"):
                return False
            return PermissionService.check_permission(user, code)

    return _HasModulePermission
