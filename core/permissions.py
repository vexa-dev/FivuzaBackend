"""Permisos DRF compartidos entre las 4 apps de negocio.

Incluye el permission_classes que verifica tenants.status antes de procesar
cualquier request de negocio -si está suspended, responde 402 sin tocar datos.
"""

from rest_framework.exceptions import APIException
from rest_framework.permissions import BasePermission

from core.models import PlatformStaff


class TenantSuspendedError(APIException):
    """402 Payment Required -formato exacto de la Especificacion de API §4.12."""

    status_code = 402
    default_code = "TENANT_SUSPENDED"
    default_detail = {
        "error": {
            "code": "TENANT_SUSPENDED",
            "message": "La suscripcion de este negocio esta suspendida. Contacta a soporte.",
        }
    }


class TenantNotSuspended(BasePermission):
    """Bloquea cualquier request de negocio si el tenant resuelto por
    subdominio esta suspended -usado por las 4 apps de negocio (usuarios,
    inventario, ventas, dashboard), nunca por los endpoints de core que
    gestionan tenants desde el panel de platform_staff."""

    def has_permission(self, request, view):
        tenant = getattr(request, "tenant", None)
        if tenant is not None and tenant.status == "suspended":
            raise TenantSuspendedError()
        return True


class IsPlatformStaff(BasePermission):
    """Solo permite el acceso a miembros autenticados del equipo interno de
    Fivuza -nunca a un usuario de tenant.users, ni siquiera un admin."""

    def has_permission(self, request, view):
        return isinstance(request.user, PlatformStaff)


def require_platform_role(*roles):
    """Factory de permiso: exige ademas que PlatformStaff.role este en roles
    (Especificacion de API §2.5, ej. "Solo platform_staff (SUPER_ADMIN)").

    Uso: permission_classes = [IsAuthenticated, require_platform_role("SUPER_ADMIN")]
    """

    class _RequirePlatformRole(BasePermission):
        def has_permission(self, request, view):
            return (
                isinstance(request.user, PlatformStaff) and request.user.role in roles
            )

    return _RequirePlatformRole
