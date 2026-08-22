from decimal import Decimal

from django.db import transaction
from django.utils import timezone
from rest_framework import serializers

from inventario.models import Warehouse
from rest_framework_simplejwt.settings import api_settings
from rest_framework_simplejwt.token_blacklist.models import OutstandingToken
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.utils import datetime_from_epoch

from usuarios.models import (
    AuditLog,
    DataExport,
    Employee,
    EmployeeAttendance,
    EmployeePayroll,
    EmployeeSchedule,
    Permission,
    Role,
    RolePermission,
    RolePermissionsHistory,
    User,
    UserPermission,
    UserWarehouse,
)
from core.warehouse_access import WarehouseAccessService


def issue_tokens_for_tenant_user(user: User, schema_name: str) -> RefreshToken:
    """Emite un RefreshToken para un tenant.users sin RefreshToken.for_user().

    Mismo problema que core.serializers.issue_tokens_for_platform_staff:
    RefreshToken.for_user() (con token_blacklist instalado) crea un
    OutstandingToken con user=<instancia>, y OutstandingToken.user es FK a
    settings.AUTH_USER_MODEL -que no es usuarios.User. Se emite el token a
    mano y se registra el OutstandingToken con user=None (el campo es
    nullable). Ademas embebe el claim schema_name -sin el,
    TenantValidatedJWTAuthentication (core/authentication.py) rechaza el
    token por no poder validar a que tenant pertenece (TRD §2.2).
    """
    refresh = RefreshToken()
    refresh[api_settings.USER_ID_CLAIM] = user.id
    refresh["schema_name"] = schema_name

    OutstandingToken.objects.create(
        user=None,
        jti=refresh[api_settings.JTI_CLAIM],
        token=str(refresh),
        created_at=refresh.current_time,
        expires_at=datetime_from_epoch(refresh["exp"]),
    )
    return refresh


class TenantUserTokenObtainSerializer(serializers.Serializer):
    """Autentica un usuario del tenant (email/password) contra el esquema ya
    resuelto por TenantMainMiddleware y emite un par de tokens JWT con el
    claim schema_name embebido (API Spec §3.1)."""

    email = serializers.EmailField()
    password = serializers.CharField(write_only=True, trim_whitespace=False)

    default_error_messages = {
        "invalid_credentials": "Correo o contraseña incorrectos.",
    }

    def validate(self, attrs):
        try:
            user = User.objects.get(email=attrs["email"], is_active=True)
        except User.DoesNotExist:
            self.fail("invalid_credentials")

        if not user.check_password(attrs["password"]):
            self.fail("invalid_credentials")

        user.last_login = timezone.now()
        user.save(update_fields=["last_login"])

        schema_name = self.context["request"].tenant.schema_name
        refresh = issue_tokens_for_tenant_user(user, schema_name)

        from usuarios.services import PermissionService

        return {
            "refresh": str(refresh),
            "access": str(refresh.access_token),
            "user": {
                "id": user.id,
                "email": user.email,
                "role": user.role.name,
                "warehouse_ids": list(
                    WarehouseAccessService.allowed_warehouse_ids(user)
                ),
                # El frontend usa esto para mostrar/ocultar secciones segun
                # permiso, nunca segun el NOMBRE del rol -los roles son
                # personalizables (Convenciones), un nombre fijo no alcanza.
                "permissions": sorted(PermissionService.get_permission_codes(user)),
            },
        }


class RoleSerializer(serializers.ModelSerializer):
    class Meta:
        model = Role
        fields = ["id", "name", "is_system_default", "description"]


class PermissionSerializer(serializers.ModelSerializer):
    """Solo lectura -el catalogo de permisos es fijo, no editable via API
    (API Spec §2.1: 'Permisos (catalogo, solo lectura)')."""

    class Meta:
        model = Permission
        fields = ["id", "code", "module", "description"]
        read_only_fields = fields


class RolePermissionSerializer(serializers.ModelSerializer):
    class Meta:
        model = RolePermission
        fields = ["id", "role", "permission"]


class RolePermissionsHistorySerializer(serializers.ModelSerializer):
    """Solo lectura -se escribe unicamente via RoleService, nunca por
    POST/PUT directo del cliente."""

    class Meta:
        model = RolePermissionsHistory
        fields = ["id", "role", "permission", "action", "changed_by", "changed_at"]
        read_only_fields = fields


class UserSerializer(serializers.ModelSerializer):
    """password es write_only y se hashea con set_password(), nunca se
    guarda en texto plano (mismo patron que PlatformStaffCRUDSerializer)."""

    password = serializers.CharField(write_only=True, required=False)
    warehouse_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1), required=False, write_only=True
    )

    class Meta:
        model = User
        fields = [
            "id",
            "email",
            "role",
            "password",
            "is_active",
            "last_login",
            "created_at",
            "warehouse_ids",
        ]
        read_only_fields = ["last_login", "created_at"]

    def to_representation(self, instance):
        data = super().to_representation(instance)
        data["warehouse_ids"] = list(
            instance.warehouse_access.order_by("warehouse_id").values_list(
                "warehouse_id", flat=True
            )
        )
        return data

    def validate_warehouse_ids(self, value):
        ids = list(dict.fromkeys(value))
        existing = set(
            Warehouse.objects.filter(id__in=ids, is_active=True).values_list(
                "id", flat=True
            )
        )
        missing = sorted(set(ids) - existing)
        if missing:
            raise serializers.ValidationError(
                f"Almacenes inexistentes o inactivos: {', '.join(map(str, missing))}."
            )
        return ids

    def validate(self, attrs):
        role = attrs.get("role", self.instance.role if self.instance else None)
        warehouse_ids = attrs.get("warehouse_ids")
        was_admin = bool(
            self.instance
            and self.instance.role.is_system_default
            and self.instance.role.name.casefold() == "admin"
        )
        will_be_admin = bool(
            role and role.is_system_default and role.name.casefold() == "admin"
        )
        if was_admin and not will_be_admin and not warehouse_ids:
            raise serializers.ValidationError(
                {
                    "warehouse_ids": "Asigna al menos un almacén al cambiar el rol administrador."
                }
            )
        return attrs

    @transaction.atomic
    def create(self, validated_data):
        warehouse_ids = validated_data.pop("warehouse_ids", [])
        password = validated_data.pop("password", None)
        instance = User(**validated_data)
        if password:
            instance.set_password(password)
        instance.save()
        self._replace_warehouses(instance, warehouse_ids)
        return instance

    @transaction.atomic
    def update(self, instance, validated_data):
        warehouse_ids = validated_data.pop("warehouse_ids", None)
        password = validated_data.pop("password", None)
        role_changed = (
            "role" in validated_data and validated_data["role"] != instance.role
        )
        for field, value in validated_data.items():
            setattr(instance, field, value)
        if password:
            instance.set_password(password)
        instance.save()
        if warehouse_ids is not None:
            self._replace_warehouses(instance, warehouse_ids)
        if role_changed:
            from usuarios.services import PermissionService

            PermissionService.invalidate_user_cache(instance.id)
        return instance

    @staticmethod
    def _replace_warehouses(instance, warehouse_ids):
        is_admin = (
            instance.role.is_system_default and instance.role.name.casefold() == "admin"
        )
        instance.warehouse_access.all().delete()
        if not is_admin:
            UserWarehouse.objects.bulk_create(
                [
                    UserWarehouse(user=instance, warehouse_id=warehouse_id)
                    for warehouse_id in warehouse_ids
                ]
            )


class UserWarehouseSerializer(serializers.ModelSerializer):
    warehouse_name = serializers.CharField(source="warehouse.name", read_only=True)

    class Meta:
        model = UserWarehouse
        fields = ["id", "user", "warehouse", "warehouse_name"]

    def validate_warehouse(self, warehouse):
        if not warehouse.is_active:
            raise serializers.ValidationError("El almacén está inactivo.")
        WarehouseAccessService.require_warehouse(
            self.context["request"].user, warehouse
        )
        return warehouse


class UserPermissionSerializer(serializers.ModelSerializer):
    class Meta:
        model = UserPermission
        fields = ["id", "user", "permission", "is_granted"]


class AuditLogSerializer(serializers.ModelSerializer):
    """Solo lectura -se escribe unicamente via AuditLogService.log_action(),
    nunca por POST/PUT directo del cliente."""

    class Meta:
        model = AuditLog
        fields = [
            "id",
            "user",
            "action",
            "entity",
            "entity_id",
            "details",
            "created_at",
        ]
        read_only_fields = fields


class EmployeeSerializer(serializers.ModelSerializer):
    class Meta:
        model = Employee
        fields = [
            "id",
            "user",
            "full_name",
            "document_number",
            "phone",
            "position",
            "warehouse",
            "salary_type",
            "salary_amount",
            "currency",
            "hire_date",
            "termination_date",
            "is_active",
            "created_at",
        ]
        read_only_fields = ["created_at"]

    def validate_warehouse(self, warehouse):
        WarehouseAccessService.require_warehouse(
            self.context["request"].user, warehouse
        )
        return warehouse


class EmployeeScheduleSerializer(serializers.ModelSerializer):
    class Meta:
        model = EmployeeSchedule
        fields = [
            "id",
            "employee",
            "day_of_week",
            "start_time",
            "end_time",
            "is_active",
        ]

    def validate_employee(self, employee):
        WarehouseAccessService.require_warehouse(
            self.context["request"].user, employee.warehouse_id
        )
        return employee


class EmployeeAttendanceSerializer(serializers.ModelSerializer):
    """Solo lectura -check_in/status se fijan via AttendanceService.clock_in(),
    check_out via clock_out(), nunca por POST/PUT directo (API Spec §4.8)."""

    worked_hours = serializers.SerializerMethodField()

    class Meta:
        model = EmployeeAttendance
        fields = [
            "id",
            "employee",
            "warehouse",
            "check_in",
            "check_out",
            "status",
            "notes",
            "worked_hours",
            "created_at",
        ]
        read_only_fields = fields

    def get_worked_hours(self, obj) -> str | None:
        from usuarios.services import AttendanceService

        hours = AttendanceService.calculate_worked_hours(obj)
        return str(hours) if hours is not None else None


class ClockInSerializer(serializers.Serializer):
    employee_id = serializers.PrimaryKeyRelatedField(
        source="employee", queryset=Employee.objects.all()
    )
    warehouse_id = serializers.PrimaryKeyRelatedField(
        source="warehouse", queryset=Warehouse.objects.all()
    )


class EmployeePayrollSerializer(serializers.ModelSerializer):
    """Solo lectura -los montos se fijan via PayrollService.generate_payroll()
    y se congelan al crearse; status/payment_date solo cambian via
    mark_paid() (Sprint 23)."""

    class Meta:
        model = EmployeePayroll
        fields = [
            "id",
            "employee",
            "period_start",
            "period_end",
            "base_salary",
            "bonuses",
            "deductions",
            "net_amount",
            "status",
            "payment_date",
            "created_at",
        ]
        read_only_fields = fields


class PayrollGenerateSerializer(serializers.Serializer):
    employee_id = serializers.PrimaryKeyRelatedField(
        source="employee", queryset=Employee.objects.all()
    )
    period_start = serializers.DateField()
    period_end = serializers.DateField()
    bonuses = serializers.DecimalField(
        max_digits=12, decimal_places=4, default=Decimal("0")
    )
    deductions = serializers.DecimalField(
        max_digits=12, decimal_places=4, default=Decimal("0")
    )

    def validate(self, attrs):
        if attrs["period_start"] > attrs["period_end"]:
            raise serializers.ValidationError(
                "El inicio del periodo debe ser antes que el fin."
            )
        return attrs


class PayrollMarkPaidSerializer(serializers.Serializer):
    payment_date = serializers.DateField(default=timezone.localdate)


class PasswordResetRequestSerializer(serializers.Serializer):
    email = serializers.EmailField()


class PasswordResetConfirmSerializer(serializers.Serializer):
    token = serializers.CharField()
    new_password = serializers.CharField(write_only=True, trim_whitespace=False)


class DataExportSerializer(serializers.ModelSerializer):
    class Meta:
        model = DataExport
        fields = [
            "id",
            "requested_by",
            "scope",
            "format",
            "status",
            "error_message",
            "requested_at",
            "completed_at",
            "expires_at",
        ]
        read_only_fields = fields


class DataExportRequestSerializer(serializers.Serializer):
    format = serializers.ChoiceField(choices=["ZIP", "XLSX"])
