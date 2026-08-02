"""Permisos DRF compartidos entre las 4 apps de negocio.

Incluye el permission_classes que verifica tenants.status antes de procesar
cualquier request de negocio -si está suspended, responde 402 sin tocar datos.
"""

from rest_framework.exceptions import APIException
from rest_framework.permissions import SAFE_METHODS, BasePermission

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


class TenantCanceledError(APIException):
    """402 Payment Required, codigo distinto de TENANT_SUSPENDED (Especificacion
    de API §4.12): un tenant cancelado no es un caso reversible, asi que el
    frontend debe distinguirlo de una suspension."""

    status_code = 402
    default_code = "TENANT_CANCELED"
    default_detail = {
        "error": {
            "code": "TENANT_CANCELED",
            "message": "Este negocio esta cancelado. Solo se permite exportar datos durante el periodo de gracia.",
        }
    }


class TenantAlreadyCanceledError(APIException):
    status_code = 409
    default_code = "TENANT_ALREADY_CANCELED"

    def __init__(self, canceled_at):
        super().__init__(
            {
                "error": {
                    "code": "TENANT_ALREADY_CANCELED",
                    "message": f"Este tenant ya fue cancelado el {canceled_at:%Y-%m-%d}.",
                }
            }
        )


class CannotReactivateCanceledTenantError(APIException):
    status_code = 409
    default_code = "CANNOT_REACTIVATE_CANCELED_TENANT"
    default_detail = {
        "error": {
            "code": "CANNOT_REACTIVATE_CANCELED_TENANT",
            "message": "Un tenant cancelado no puede reactivarse. Si el negocio desea volver, debe registrarse como un tenant nuevo.",
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


class TenantNotCanceled(BasePermission):
    """A diferencia de TenantNotSuspended (bloquea todo, incluidas lecturas),
    un tenant cancelado conserva lectura durante su periodo de gracia de 30
    dias -para que el negocio pueda exportar su respaldo (Especificacion de
    API §4.12: "solo lectura/exportacion"). Solo bloquea metodos de escritura."""

    def has_permission(self, request, view):
        tenant = getattr(request, "tenant", None)
        if (
            tenant is not None
            and tenant.status == "canceled"
            and request.method not in SAFE_METHODS
        ):
            raise TenantCanceledError()
        return True


class IsPlatformStaff(BasePermission):
    """Solo permite el acceso a miembros autenticados del equipo interno de
    Fivuza -nunca a un usuario de tenant.users, ni siquiera un admin."""

    def has_permission(self, request, view):
        return isinstance(request.user, PlatformStaff)


class ModuleDisabledError(APIException):
    """403 con codigo MODULE_DISABLED -mismo formato ya usado a mano en
    ProductViewSet/WarehouseViewSet (Sprint 3); se centraliza aqui para que
    RequiresFeature() no tenga que repetir el dict a mano cada vez."""

    status_code = 403
    default_code = "MODULE_DISABLED"

    def __init__(self, feature_code: str):
        super().__init__(
            {
                "code": "MODULE_DISABLED",
                "feature": feature_code,
                "message": "Este modulo no esta disponible en tu plan/configuracion actual.",
            }
        )


def RequiresFeature(feature_code: str):
    """Factory de permiso: bloquea el ViewSet completo si FeatureFlagService
    dice que `feature_code` esta apagado para el tenant (Esquema Backend
    §8.2). Usar junto con el permiso de modulo (ej. HasModulePermission)
    para que un usuario sin el permiso vea 403 "sin permiso" y no 403
    "modulo apagado" -el orden en permission_classes importa: DRF evalua
    en orden y devuelve el primer has_permission() que falle."""

    class _RequiresFeature(BasePermission):
        def has_permission(self, request, view):
            from core.services import FeatureFlagService

            tenant = getattr(request, "tenant", None)
            if tenant is not None and not FeatureFlagService.is_enabled(
                tenant, feature_code
            ):
                raise ModuleDisabledError(feature_code)
            return True

    return _RequiresFeature


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
