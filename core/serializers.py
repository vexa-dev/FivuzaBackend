from django.utils import timezone
from rest_framework import serializers
from rest_framework_simplejwt.settings import api_settings
from rest_framework_simplejwt.token_blacklist.models import OutstandingToken
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.utils import datetime_from_epoch

from core.models import (
    Domain,
    PlatformAuditLog,
    PlatformStaff,
    Plan,
    PlanFeature,
    Subscription,
    SubscriptionDiscount,
    SubscriptionPayment,
    Tenant,
    TenantFeatureOverride,
    TenantNote,
    TenantSettings,
)
from core.services import TenantRegistrationService


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
            # Especificacion de API §3.2: la respuesta de login debe incluir
            # el staff autenticado -sin esto el frontend no tiene forma de
            # saber su rol (SUPER_ADMIN/SUPPORT/BILLING) para, por ejemplo,
            # ocultar secciones del panel segun permisos (Sprint 9).
            "staff": {
                "id": user.id,
                "email": user.email,
                "full_name": user.full_name,
                "role": user.role,
            },
        }


class TenantSerializer(serializers.ModelSerializer):
    """Sin campo password/create: el registro de un tenant nuevo es un
    endpoint de accion propio (Especificacion de API §4.9), fuera del
    alcance de este CRUD (Sprint 1, tarea 5)."""

    domain = serializers.SerializerMethodField()

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
            "canceled_at",
            "provisioning_status",
            "created_on",
            "domain",
        ]
        read_only_fields = [
            "status",
            "suspended_at",
            "canceled_at",
            "provisioning_status",
            "created_on",
            "domain",
        ]

    def get_domain(self, tenant: Tenant) -> str | None:
        # Sprint 10: el panel core necesita el dominio del tenant para
        # redirigir el navegador a su subdominio al iniciar una sesion de
        # soporte (impersonacion) -no existia ningun campo expuesto con esto.
        domain = tenant.domains.filter(is_primary=True).first()
        return domain.domain if domain else None


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
            "cash_difference_alert_threshold",
            "dashboard_refresh_minutes",
            "updated_at",
        ]
        read_only_fields = ["updated_at"]


class TenantFeatureOverrideSerializer(serializers.ModelSerializer):
    class Meta:
        model = TenantFeatureOverride
        fields = ["id", "tenant", "feature_code", "is_enabled"]
        read_only_fields = ["tenant", "feature_code"]


class TenantNoteSerializer(serializers.ModelSerializer):
    """Solo lectura -se crea via TenantNoteService.add_note() en la vista,
    nunca aceptando tenant/platform_staff del payload del cliente."""

    platform_staff = serializers.SerializerMethodField()

    class Meta:
        model = TenantNote
        fields = ["id", "tenant", "platform_staff", "text", "created_at"]
        read_only_fields = fields

    def get_platform_staff(self, note: TenantNote) -> dict:
        return {
            "id": note.platform_staff_id,
            "full_name": note.platform_staff.full_name,
        }


class SubscriptionDiscountSerializer(serializers.ModelSerializer):
    subscription_id = serializers.IntegerField(read_only=True)

    class Meta:
        model = SubscriptionDiscount
        fields = [
            "id",
            "subscription_id",
            "discount_percent",
            "override_price",
            "reason",
            "expires_at",
            "created_at",
        ]
        read_only_fields = fields


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


class PlatformAuditLogSerializer(serializers.ModelSerializer):
    """Solo lectura (Especificacion de API §4.14) -platform_audit_logs se
    escribe unicamente via PlatformAuditLogService.log_action(), nunca por
    POST/PUT directo del cliente."""

    platform_staff_email = serializers.EmailField(
        source="platform_staff.email", read_only=True
    )

    class Meta:
        model = PlatformAuditLog
        fields = [
            "id",
            "platform_staff",
            "platform_staff_email",
            "action",
            "entity",
            "entity_id",
            "details",
            "created_at",
        ]
        read_only_fields = fields


class TenantRegisterSerializer(serializers.Serializer):
    """POST /api/v1/core/tenants/register/ (Especificacion de API §4.9).

    No es un ModelSerializer porque el payload combina campos de 3 modelos
    distintos (Tenant, Domain, Subscription) -delega la orquestacion a
    TenantRegistrationService."""

    company_name = serializers.CharField(max_length=100)
    ruc = serializers.CharField(max_length=20, required=False, allow_null=True)
    schema_name = serializers.CharField(max_length=63)
    domain = serializers.CharField(max_length=253)
    plan_code = serializers.CharField()
    billing_cycle = serializers.ChoiceField(choices=["MONTHLY", "SEMIANNUAL", "ANNUAL"])

    def validate_schema_name(self, value):
        if Tenant.objects.filter(schema_name=value).exists():
            raise serializers.ValidationError(
                "Ya existe un tenant con ese schema_name."
            )
        return value

    def validate_domain(self, value):
        if Domain.objects.filter(domain=value).exists():
            raise serializers.ValidationError("Ya existe un tenant con ese dominio.")
        return value

    def validate_plan_code(self, value):
        if not Plan.objects.filter(code=value, is_active=True).exists():
            raise serializers.ValidationError(
                "No existe un plan activo con ese codigo."
            )
        return value

    def create(self, validated_data):
        return TenantRegistrationService.register(**validated_data)
