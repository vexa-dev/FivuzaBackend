from django.utils import timezone
from rest_framework import serializers
from rest_framework_simplejwt.settings import api_settings
from rest_framework_simplejwt.token_blacklist.models import OutstandingToken
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.utils import datetime_from_epoch

from core.models import PlatformStaff


def issue_tokens_for_platform_staff(user: PlatformStaff) -> RefreshToken:
    """Emite un RefreshToken para un PlatformStaff sin usar RefreshToken.for_user().

    RefreshToken.for_user() (con token_blacklist instalado) crea un
    OutstandingToken con user=<instancia>, y OutstandingToken.user es FK a
    settings.AUTH_USER_MODEL -que aqui es el User nativo de Django, no
    PlatformStaff. Se emite el token a mano y se registra el OutstandingToken
    con user=None (el campo es nullable) para que el blacklist en logout
    (que solo depende del jti, no del FK user) siga funcionando igual.
    """
    refresh = RefreshToken()
    refresh[api_settings.USER_ID_CLAIM] = user.id

    OutstandingToken.objects.create(
        user=None,
        jti=refresh[api_settings.JTI_CLAIM],
        token=str(refresh),
        created_at=refresh.current_time,
        expires_at=datetime_from_epoch(refresh["exp"]),
    )
    return refresh


class PlatformStaffTokenObtainSerializer(serializers.Serializer):
    """Autentica un miembro del equipo Fivuza por email/password y emite un par
    de tokens JWT. No usa TokenObtainPairSerializer de simplejwt directamente
    porque ese serializer asume AUTH_USER_MODEL; PlatformStaff es un modelo
    aparte (Esquema Backend, mejora de arquitectura #2)."""

    email = serializers.EmailField()
    password = serializers.CharField(write_only=True, trim_whitespace=False)

    default_error_messages = {
        "invalid_credentials": "Credenciales invalidas.",
    }

    def validate(self, attrs):
        try:
            user = PlatformStaff.objects.get(email=attrs["email"], is_active=True)
        except PlatformStaff.DoesNotExist:
            self.fail("invalid_credentials")

        if not user.check_password(attrs["password"]):
            self.fail("invalid_credentials")

        user.last_login = timezone.now()
        user.save(update_fields=["last_login"])

        refresh = issue_tokens_for_platform_staff(user)
        return {
            "refresh": str(refresh),
            "access": str(refresh.access_token),
        }
