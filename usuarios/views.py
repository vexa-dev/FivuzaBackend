from django.conf import settings
from django.core.exceptions import ValidationError as DjangoValidationError
from django.utils import timezone
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import RefreshToken

from core.permissions import RequiresFeature, TenantNotCanceled, TenantNotSuspended
from usuarios.models import (
    AuditLog,
    Employee,
    EmployeeAttendance,
    EmployeeSchedule,
    Permission,
    Role,
    RolePermission,
    RolePermissionsHistory,
    User,
    UserPermission,
)
from usuarios.permissions import HasModulePermission
from usuarios.serializers import (
    AuditLogSerializer,
    ClockInSerializer,
    EmployeeAttendanceSerializer,
    EmployeeScheduleSerializer,
    EmployeeSerializer,
    PasswordResetConfirmSerializer,
    PasswordResetRequestSerializer,
    PermissionSerializer,
    RolePermissionSerializer,
    RolePermissionsHistorySerializer,
    RoleSerializer,
    TenantUserTokenObtainSerializer,
    UserPermissionSerializer,
    UserSerializer,
)
from usuarios.services import (
    AttendanceService,
    AuditLogService,
    PasswordResetService,
    PermissionService,
    RoleService,
)

_HR_PERMISSIONS = [
    IsAuthenticated,
    TenantNotSuspended,
    TenantNotCanceled,
    RequiresFeature("HAS_HR_MODULE"),
    HasModulePermission("HR_MANAGE"),
]


class TenantUserLoginView(APIView):
    """POST email/password de un usuario del tenant -> par de tokens JWT
    (API Spec §3.1). Resuelve el tenant por el subdominio de la request,
    ya procesado por TenantMainMiddleware antes de llegar aqui."""

    permission_classes = [AllowAny]

    def post(self, request):
        serializer = TenantUserTokenObtainSerializer(
            data=request.data, context={"request": request}
        )
        serializer.is_valid(raise_exception=True)
        return Response(serializer.validated_data, status=status.HTTP_200_OK)


class TenantUserLogoutView(APIView):
    """POST refresh token -> lo agrega a la blacklist, invalidandolo."""

    permission_classes = [IsAuthenticated]

    def post(self, request):
        refresh_token = request.data.get("refresh")
        if not refresh_token:
            raise ValidationError({"refresh": "Este campo es requerido."})
        try:
            RefreshToken(refresh_token).blacklist()
        except TokenError as exc:
            raise ValidationError({"refresh": str(exc)})
        return Response(status=status.HTTP_205_RESET_CONTENT)


class PasswordResetRequestView(APIView):
    """POST email -> siempre responde 200, exista o no ese correo (nunca
    revela si un correo esta registrado). El envio real es asincrono via
    Celery, la respuesta HTTP no espera a que el correo salga."""

    permission_classes = [AllowAny]

    def post(self, request):
        serializer = PasswordResetRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        hostname = request.get_host().split(":")[0]
        port_suffix = f":{settings.FRONTEND_PORT}" if settings.FRONTEND_PORT else ""
        frontend_origin = f"{settings.FRONTEND_SCHEME}://{hostname}{port_suffix}"

        PasswordResetService.request_reset(
            email=serializer.validated_data["email"],
            schema_name=request.tenant.schema_name,
            frontend_origin=frontend_origin,
        )
        return Response(status=status.HTTP_200_OK)


class PasswordResetConfirmView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = PasswordResetConfirmSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            PasswordResetService.confirm_reset(
                token=serializer.validated_data["token"],
                new_password=serializer.validated_data["new_password"],
            )
        except DjangoValidationError as exc:
            raise ValidationError(exc.message) from exc
        return Response(status=status.HTTP_200_OK)


class RoleViewSet(viewsets.ModelViewSet):
    """Roles a medida (ej. "Cajero", "Limpieza"): el negocio los crea y les
    concede permisos vía RolePermissionViewSet (API Spec §2.1). destroy()
    delega en RoleService.delete_role() -nunca es un DELETE fisico, ver
    docstring de Role."""

    queryset = Role.objects.all()
    serializer_class = RoleSerializer
    permission_classes = [IsAuthenticated, HasModulePermission("USERS_MANAGE_ROLES")]

    def perform_destroy(self, instance):
        RoleService.delete_role(instance, deleted_by=self.request.user)


class PermissionViewSet(viewsets.ReadOnlyModelViewSet):
    """Catalogo fijo de permisos -no editable via API (API Spec §2.1)."""

    queryset = Permission.objects.all()
    serializer_class = PermissionSerializer
    permission_classes = [IsAuthenticated]


class RolePermissionViewSet(viewsets.ModelViewSet):
    """Asignacion de permisos a un rol. create/destroy pasan siempre por
    RoleService para dejar registro en RolePermissionsHistory (Esquema
    Backend §4.2) -nunca se inserta/borra la fila directamente."""

    queryset = RolePermission.objects.all()
    serializer_class = RolePermissionSerializer
    permission_classes = [IsAuthenticated, HasModulePermission("USERS_MANAGE_ROLES")]

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        role_permission = RoleService.grant_permission(
            role=serializer.validated_data["role"],
            permission=serializer.validated_data["permission"],
            changed_by=request.user,
        )
        AuditLogService.log_action(
            user=request.user,
            action="USER_ROLE_CHANGED",
            entity="Role",
            entity_id=role_permission.role_id,
            details={"granted": role_permission.permission.code},
        )
        output = self.get_serializer(role_permission)
        return Response(output.data, status=status.HTTP_201_CREATED)

    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()
        role_id, permission = instance.role_id, instance.permission
        RoleService.revoke_permission(
            role=instance.role, permission=permission, changed_by=request.user
        )
        AuditLogService.log_action(
            user=request.user,
            action="USER_ROLE_CHANGED",
            entity="Role",
            entity_id=role_id,
            details={"revoked": permission.code},
        )
        return Response(status=status.HTTP_204_NO_CONTENT)


class RolePermissionsHistoryViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = RolePermissionsHistory.objects.all()
    serializer_class = RolePermissionsHistorySerializer
    permission_classes = [IsAuthenticated, HasModulePermission("USERS_VIEW_AUDIT")]


class UserViewSet(viewsets.ModelViewSet):
    queryset = User.objects.all()
    serializer_class = UserSerializer
    permission_classes = [IsAuthenticated, HasModulePermission("USERS_MANAGE")]

    def perform_destroy(self, instance):
        instance.deleted_at = timezone.now()
        instance.deleted_by = self.request.user
        instance.is_active = False
        instance.save(update_fields=["deleted_at", "deleted_by", "is_active"])
        PermissionService.invalidate_user_cache(instance.id)


class UserPermissionViewSet(viewsets.ModelViewSet):
    queryset = UserPermission.objects.all()
    serializer_class = UserPermissionSerializer
    permission_classes = [IsAuthenticated, HasModulePermission("USERS_MANAGE")]

    def perform_create(self, serializer):
        super().perform_create(serializer)
        PermissionService.invalidate_user_cache(serializer.instance.user_id)

    def perform_update(self, serializer):
        super().perform_update(serializer)
        PermissionService.invalidate_user_cache(serializer.instance.user_id)

    def perform_destroy(self, instance):
        user_id = instance.user_id
        super().perform_destroy(instance)
        PermissionService.invalidate_user_cache(user_id)


class AuditLogViewSet(viewsets.ReadOnlyModelViewSet):
    """Solo lectura -se escribe unicamente via AuditLogService.log_action(),
    nunca por POST/PUT directo del cliente (API Spec §2.1)."""

    queryset = AuditLog.objects.all().order_by("-created_at")
    serializer_class = AuditLogSerializer
    permission_classes = [IsAuthenticated, HasModulePermission("USERS_VIEW_AUDIT")]


class EmployeeViewSet(viewsets.ModelViewSet):
    """Ficha de trabajador (Sprint 22). Gateado por HAS_HR_MODULE ademas de
    HR_MANAGE -a diferencia de compras (activo por defecto), RRHH arranca
    apagado (tenant_settings.hr_module_enabled=False), asi que el modulo
    completo esta oculto hasta que el negocio lo activa."""

    queryset = Employee.objects.select_related("user", "warehouse")
    serializer_class = EmployeeSerializer
    permission_classes = _HR_PERMISSIONS

    def get_queryset(self):
        queryset = super().get_queryset().order_by("full_name")
        warehouse_id = self.request.query_params.get("warehouse")
        if warehouse_id:
            queryset = queryset.filter(warehouse_id=warehouse_id)
        search = self.request.query_params.get("search")
        if search:
            queryset = queryset.filter(full_name__icontains=search)
        return queryset

    def perform_destroy(self, instance):
        instance.deleted_at = timezone.now()
        instance.deleted_by = self.request.user
        instance.is_active = False
        instance.save(update_fields=["deleted_at", "deleted_by", "is_active"])


class EmployeeScheduleViewSet(viewsets.ModelViewSet):
    """Horario programado por trabajador -vista semanal en el frontend
    (Sprint 22, Convenciones §5.1)."""

    queryset = EmployeeSchedule.objects.select_related("employee")
    serializer_class = EmployeeScheduleSerializer
    permission_classes = _HR_PERMISSIONS

    def get_queryset(self):
        queryset = super().get_queryset().order_by("employee_id", "day_of_week")
        employee_id = self.request.query_params.get("employee")
        if employee_id:
            queryset = queryset.filter(employee_id=employee_id)
        return queryset


class EmployeeAttendanceViewSet(
    mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet
):
    """Solo lectura -entrada/salida son acciones propias (clock-in/clock-out),
    no un create/update generico (API Spec §4.8), mismo criterio que
    CashSessionViewSet (Sprint 12)."""

    queryset = EmployeeAttendance.objects.select_related("employee", "warehouse")
    serializer_class = EmployeeAttendanceSerializer
    permission_classes = _HR_PERMISSIONS

    def get_queryset(self):
        queryset = super().get_queryset().order_by("-check_in")
        employee_id = self.request.query_params.get("employee")
        if employee_id:
            queryset = queryset.filter(employee_id=employee_id)
        return queryset

    @action(detail=False, methods=["post"], url_path="clock-in")
    def clock_in(self, request):
        serializer = ClockInSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        attendance = AttendanceService.clock_in(
            employee=serializer.validated_data["employee"],
            warehouse=serializer.validated_data["warehouse"],
            user=request.user,
        )
        return Response(
            {
                "id": attendance.id,
                "check_in": attendance.check_in,
                "status": attendance.status,
            },
            status=status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=["post"], url_path="clock-out")
    def clock_out(self, request, pk=None):
        attendance = self.get_object()
        attendance = AttendanceService.clock_out(
            attendance=attendance, user=request.user
        )
        return Response(
            {"id": attendance.id, "check_out": attendance.check_out},
            status=status.HTTP_200_OK,
        )
