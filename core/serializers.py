from django.utils import timezone
from rest_framework import serializers
from rest_framework_simplejwt.settings import api_settings
from rest_framework_simplejwt.token_blacklist.models import OutstandingToken
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.utils import datetime_from_epoch

from core.models import (
    PlatformStaff,
    Plan,
    PlanFeature,
    Subscription,
    SubscriptionPayment,
    Tenant,
    TenantSettings,
)


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


class TenantSerializer(serializers.ModelSerializer):
    """Sin campo password/create: el registro de un tenant nuevo es un
    endpoint de accion propio (Especificacion de API §4.9), fuera del
    alcance de este CRUD (Sprint 1, tarea 5)."""

    class Meta:
        model = Tenant
        fields = [
            "id",
            "schema_name",
            "company_name",
            "ruc",
            "default_currency",
            "status",
            "suspended_at",
            "created_on",
        ]
        read_only_fields = ["status", "suspended_at", "created_on"]


class PlanSerializer(serializers.ModelSerializer):
    class Meta:
        model = Plan
        fields = [
            "id",
            "code",
            "name",
            "max_users",
            "price_monthly",
            "price_semiannual",
            "price_annual",
            "is_active",
        ]


class PlanFeatureSerializer(serializers.ModelSerializer):
    class Meta:
        model = PlanFeature
        fields = ["id", "plan", "feature_code", "is_enabled"]


class SubscriptionSerializer(serializers.ModelSerializer):
    class Meta:
        model = Subscription
        fields = [
            "id",
            "tenant",
            "plan",
            "billing_cycle",
            "price_paid",
            "currency",
            "status",
            "starts_at",
            "expires_at",
            "created_at",
        ]
        read_only_fields = ["created_at"]


class SubscriptionPaymentSerializer(serializers.ModelSerializer):
    class Meta:
        model = SubscriptionPayment
        fields = [
            "id",
            "subscription",
            "amount",
            "currency",
            "payment_method",
            "external_reference",
            "status",
            "paid_at",
            "created_at",
        ]
        read_only_fields = ["created_at"]


class TenantSettingsSerializer(serializers.ModelSerializer):
    class Meta:
        model = TenantSettings
        fields = [
            "id",
            "tenant",
            "purchases_enabled",
            "variants_enabled",
            "multi_warehouse_enabled",
            "hr_module_enabled",
            "cash_module_enabled",
            "updated_at",
        ]
        read_only_fields = ["updated_at"]


class PlatformStaffCRUDSerializer(serializers.ModelSerializer):
    """Distinto de PlatformStaffTokenObtainSerializer (login): este es el CRUD
    del equipo interno de Fivuza. password es write_only y se hashea con
    set_password(), nunca se guarda en texto plano."""

    password = serializers.CharField(write_only=True, required=False)

    class Meta:
        model = PlatformStaff
        fields = [
            "id",
            "email",
            "full_name",
            "role",
            "password",
            "is_active",
            "last_login",
            "created_at",
        ]
        read_only_fields = ["last_login", "created_at"]

    def create(self, validated_data):
        password = validated_data.pop("password", None)
        instance = PlatformStaff(**validated_data)
        if password:
            instance.set_password(password)
        instance.save()
        return instance

    def update(self, instance, validated_data):
        password = validated_data.pop("password", None)
        for field, value in validated_data.items():
            setattr(instance, field, value)
        if password:
            instance.set_password(password)
        instance.save()
        return instance
