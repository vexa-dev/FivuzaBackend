"""Permisos DRF específicos de inventario, además de los compartidos de usuarios."""

from rest_framework.permissions import SAFE_METHODS, BasePermission

from usuarios.services import PermissionService


class HasInventoryAccess(BasePermission):
    """El mismo ViewSet de catálogo sirve tanto a quien solo necesita
    consultarlo (INVENTORY_VIEW, ej. un vendedor) como a quien lo administra
    (INVENTORY_MANAGE, ej. un admin/manager) -se consulta con mucha más
    frecuencia de la que se edita, así que separar en dos ViewSets sería
    duplicar código sin necesidad real."""

    def has_permission(self, request, view):
        user = request.user
        if not hasattr(user, "role_id"):
            return False
        if request.method in SAFE_METHODS:
            return PermissionService.check_permission(
                user, "INVENTORY_VIEW"
            ) or PermissionService.check_permission(user, "INVENTORY_MANAGE")
        return PermissionService.check_permission(user, "INVENTORY_MANAGE")
