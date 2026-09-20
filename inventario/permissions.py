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


def viewer_sees_cost(request) -> bool:
    """Bloque A.5: INVENTORY_VIEW_COST separa "ver el catalogo" de "ver
    cuanto nos cuesta". Lo consultan los serializers y los reportes que
    exponen costo, valorizacion o margen.

    Permiso propio y no efectivo: los interruptores de caja (Bloque A.0) no
    tienen nada que ver con costos.
    """
    user = getattr(request, "user", None)
    if user is None or not hasattr(user, "role_id"):
        return False
    return PermissionService.check_permission(user, "INVENTORY_VIEW_COST")


class HasCostAccess(BasePermission):
    """Reportes que son costo puro (valorizacion de stock): sin
    INVENTORY_VIEW_COST no hay nada que mostrar, asi que se corta en la
    puerta con 403 en vez de devolver un reporte vacio."""

    def has_permission(self, request, view):
        return viewer_sees_cost(request)
