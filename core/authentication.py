from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.exceptions import AuthenticationFailed

from core.models import PlatformStaff


class TenantValidatedJWTAuthentication(JWTAuthentication):
    """Autentica tokens de tenant.users, usada por las 4 apps de negocio.

    Ademas de validar firma y vigencia (JWTAuthentication estandar), verifica
    que el claim schema_name del token coincida con request.tenant.schema_name
    -ya resuelto por TenantMainMiddleware antes de llegar aqui- para impedir
    activamente que un token valido de un tenant se use contra el subdominio
    de otro (TRD, seccion 2.2; Esquema Backend, seccion 2.4).

    Importa usuarios.User de forma perezosa (dentro de get_user, no a nivel de
    modulo): es la misma excepcion deliberada que TenantProvisioningService
    -infraestructura de autenticacion compartida que necesita conocer el
    modelo concreto de usuario, no logica de negocio de la app usuarios.
    """

    def authenticate(self, request):
        header = self.get_header(request)
        if header is None:
            return None
        raw_token = self.get_raw_token(header)
        if raw_token is None:
            return None
        validated_token = self.get_validated_token(raw_token)

        if "schema_name" not in validated_token:
            # Token de platform_staff, no de tenant.users -lo resuelve
            # PlatformStaffJWTAuthentication, no esta clase.
            return None

        token_schema = validated_token.get("schema_name")
        request_schema = getattr(getattr(request, "tenant", None), "schema_name", None)
        if token_schema != request_schema:
            raise AuthenticationFailed(
                "El token no corresponde a este tenant", code="tenant_mismatch"
            )
        return self.get_user(validated_token), validated_token

    def get_user(self, validated_token):
        from usuarios.models import User as TenantUser

        user_id = validated_token.get("user_id")
        if user_id is None:
            raise AuthenticationFailed(
                "Token sin user_id", code="token_missing_user_id"
            )
        try:
            return TenantUser.objects.get(id=user_id, is_active=True)
        except TenantUser.DoesNotExist:
            raise AuthenticationFailed(
                "Usuario no encontrado o inactivo", code="user_not_found"
            )


class PlatformStaffJWTAuthentication(JWTAuthentication):
    """Autentica tokens emitidos para el equipo interno de Fivuza (public.platform_staff).

    Flujo completamente separado del de tenant.users (Esquema Backend, mejora de
    arquitectura #2): resuelve el claim user_id contra PlatformStaff, nunca contra
    AUTH_USER_MODEL ni contra tenant.users.
    """

    def authenticate(self, request):
        header = self.get_header(request)
        if header is None:
            return None
        raw_token = self.get_raw_token(header)
        if raw_token is None:
            return None
        validated_token = self.get_validated_token(raw_token)

        if "schema_name" in validated_token:
            # Token de tenant.users, no de platform_staff -evita que un
            # user_id de tenant.users que coincida por casualidad con un id
            # de platform_staff se autentique como si fuera del equipo Fivuza.
            return None

        return self.get_user(validated_token), validated_token

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
