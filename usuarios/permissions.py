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
            # Efectivo y no propio: un interruptor del negocio (Bloque A.0)
            # puede conceder CASH_OPEN/CASH_CLOSE a quien ya vende, sin que
            # su rol los liste.
            return PermissionService.check_effective_permission(user, code)

    return _HasModulePermission


def HasPermissionOrSupervisorAuthorization(code):
    """Bloque C.1: como HasModulePermission(code), pero quien vende
    (SALES_MANAGE) y no tiene `code` pasa si trae la cabecera de
    autorizacion de un supervisor. Aqui solo se mira que la cabecera este:
    el token se valida y se gasta en el servicio, dentro de la transaccion
    de la operacion. Sin cabecera responde el 403 que abre el modal."""

    class _HasPermissionOrSupervisorAuthorization(BasePermission):
        def has_permission(self, request, view):
            from usuarios.authorization import (
                SupervisorAuthorizationRequiredError,
                authorization_token_from,
            )

            user = request.user
            if not hasattr(user, "role_id"):
                return False
            if PermissionService.check_effective_permission(user, code):
                return True
            if not PermissionService.check_effective_permission(user, "SALES_MANAGE"):
                return False
            if authorization_token_from(request) is None:
                raise SupervisorAuthorizationRequiredError(code)
            return True

    return _HasPermissionOrSupervisorAuthorization
