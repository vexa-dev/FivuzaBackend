from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.exceptions import AuthenticationFailed

from core.models import PlatformStaff


class PlatformStaffJWTAuthentication(JWTAuthentication):
    """Autentica tokens emitidos para el equipo interno de Fivuza (public.platform_staff).

    Flujo completamente separado del de tenant.users (Esquema Backend, mejora de
    arquitectura #2): resuelve el claim user_id contra PlatformStaff, nunca contra
    AUTH_USER_MODEL ni contra tenant.users.
    """

    def get_user(self, validated_token):
        user_id = validated_token.get("user_id")
        if user_id is None:
            raise AuthenticationFailed(
                "Token sin user_id", code="token_missing_user_id"
            )
        try:
            return PlatformStaff.objects.get(id=user_id, is_active=True)
        except PlatformStaff.DoesNotExist:
            raise AuthenticationFailed(
                "Usuario del equipo Fivuza no encontrado o inactivo",
                code="platform_staff_not_found",
            )
