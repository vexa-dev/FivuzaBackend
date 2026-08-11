from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError as DjangoValidationError
from django.shortcuts import get_object_or_404
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
from core.throttling import LoginRateThrottle
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
)
from usuarios.permissions import HasModulePermission
from usuarios.serializers import (
    AuditLogSerializer,
    ClockInSerializer,
    DataExportRequestSerializer,
    DataExportSerializer,
    EmployeeAttendanceSerializer,
    EmployeePayrollSerializer,
    EmployeeScheduleSerializer,
    EmployeeSerializer,
    PasswordResetConfirmSerializer,
    PasswordResetRequestSerializer,
    PayrollGenerateSerializer,
    PayrollMarkPaidSerializer,
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
    PayrollService,
    PermissionService,
    PersonalDataService,
    ReportExportService,
    RoleService,
    TenantDataExportService,
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
    throttle_classes = [LoginRateThrottle]

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


class OwnDataExportView(APIView):
    """GET -> derecho de Acceso (ARCO, Sprint 33, Ley N 29733): un usuario
    exporta sus propios datos personales (perfil, permisos, ficha de
    empleado si tiene una vinculada). Sincrono -es un solo usuario, no el
    negocio completo (eso es TenantDataExportService)."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response(PersonalDataService.export_own_data(request.user))


class UserAnonymizeView(APIView):
    """POST -> derecho de Rectificacion/Cancelacion/Oposicion (ARCO): un
    admin/soporte anonimiza los datos personales de un usuario a pedido
    (email, nombre, telefono, documento). No es un hard delete -User esta
    protegido por PROTECT desde casi todas las apps de negocio."""

    permission_classes = [IsAuthenticated, HasModulePermission("USERS_MANAGE")]

    def post(self, request, pk=None):
        user = get_object_or_404(User, pk=pk)
        user = PersonalDataService.anonymize_user(user)
        AuditLogService.log_action(
            user=request.user,
            action="USER_ANONYMIZED",
            entity="User",
            entity_id=user.id,
        )
        return Response(UserSerializer(user).data)


_DATA_EXPORT_PERMISSIONS = [
    IsAuthenticated,
    TenantNotSuspended,
    TenantNotCanceled,
    HasModulePermission("DATA_EXPORT"),
]


class DataExportViewSet(
    mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet
):
    """Respaldo completo del negocio (Sprint 33, Ley N 29733, API Spec
    §4.17). Sin update/destroy -el ciclo de vida de un DataExport lo
    maneja unicamente generate_data_export/expire_data_exports."""

    queryset = DataExport.objects.all().order_by("-requested_at")
    serializer_class = DataExportSerializer
    permission_classes = _DATA_EXPORT_PERMISSIONS

    def create(self, request, *args, **kwargs):
        serializer = DataExportRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        export = TenantDataExportService.request_export(
            user=request.user, format=serializer.validated_data["format"]
        )
        return Response(
            DataExportSerializer(export).data, status=status.HTTP_201_CREATED
        )

    @action(detail=True, methods=["get"], url_path="download")
    def download(self, request, pk=None):
        export = get_object_or_404(self.get_queryset(), pk=pk)
        url = TenantDataExportService.get_download_url(export)
        return Response({"download_url": url})


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


class EmployeePayrollViewSet(
    mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet
):
    """Solo lectura -generar y marcar pagado son acciones propias, no un
    create/update generico (Sprint 23, mismo criterio que
    EmployeeAttendanceViewSet). Los montos se congelan al generarse: no
    existe accion de recalculo, editar una planilla ya creada."""

    queryset = EmployeePayroll.objects.select_related("employee")
    serializer_class = EmployeePayrollSerializer
    permission_classes = _HR_PERMISSIONS

    def get_queryset(self):
        queryset = super().get_queryset().order_by("-period_start")
        employee_id = self.request.query_params.get("employee")
        if employee_id:
            queryset = queryset.filter(employee_id=employee_id)
        return queryset

    @action(detail=False, methods=["post"])
    def generate(self, request):
        serializer = PayrollGenerateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        payroll = PayrollService.generate_payroll(
            employee=serializer.validated_data["employee"],
            period_start=serializer.validated_data["period_start"],
            period_end=serializer.validated_data["period_end"],
            bonuses=serializer.validated_data["bonuses"],
            deductions=serializer.validated_data["deductions"],
            user=request.user,
        )
        return Response(
            EmployeePayrollSerializer(payroll).data, status=status.HTTP_201_CREATED
        )

    @action(detail=True, methods=["post"], url_path="mark-paid")
    def mark_paid(self, request, pk=None):
        payroll = self.get_object()
        serializer = PayrollMarkPaidSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        payroll = PayrollService.mark_paid(
            payroll=payroll,
            payment_date=serializer.validated_data["payment_date"],
            user=request.user,
        )
        return Response(EmployeePayrollSerializer(payroll).data)


class AttendanceReportView(APIView):
    """GET /usuarios/reports/attendance/?date_from=&date_to=&employee=&export=
    (Sprint 23, API Spec §2.1). Resumen de asistencia por trabajador en un
    rango de fechas. Con ?export=csv|xlsx devuelve el archivo en vez del
    JSON -misma consulta en ambos casos (ReportExportService, §4.16).
    Deliberadamente NO se llama "format" -ese nombre esta reservado por
    DRF (URL_FORMAT_OVERRIDE) para su propia negociacion de contenido; un
    ?format=csv sin un renderer CSV registrado devuelve 404 en vez de
    llegar a esta vista (bug real encontrado al verificar este endpoint)."""

    permission_classes = _HR_PERMISSIONS

    def get(self, request):
        date_from = request.query_params.get("date_from")
        date_to = request.query_params.get("date_to")
        if not date_from or not date_to:
            raise ValidationError("date_from y date_to son requeridos.")

        queryset = EmployeeAttendance.objects.select_related("employee").filter(
            check_in__date__gte=date_from, check_in__date__lte=date_to
        )
        employee_id = request.query_params.get("employee")
        if employee_id:
            queryset = queryset.filter(employee_id=employee_id)

        summary: dict[int, dict] = {}
        for entry in queryset:
            bucket = summary.setdefault(
                entry.employee_id,
                {
                    "employee_id": entry.employee_id,
                    "full_name": entry.employee.full_name,
                    "on_time_count": 0,
                    "late_count": 0,
                    "absence_justified_count": 0,
                    "absence_unjustified_count": 0,
                    "total_worked_hours": Decimal("0"),
                },
            )
            key = {
                "ON_TIME": "on_time_count",
                "LATE": "late_count",
                "ABSENCE_JUSTIFIED": "absence_justified_count",
                "ABSENCE_UNJUSTIFIED": "absence_unjustified_count",
            }.get(entry.status)
            if key:
                bucket[key] += 1
            worked = AttendanceService.calculate_worked_hours(entry)
            if worked is not None:
                bucket["total_worked_hours"] += worked

        rows = sorted(summary.values(), key=lambda row: row["full_name"])
        for row in rows:
            row["total_worked_hours"] = str(row["total_worked_hours"])

        export_format = request.query_params.get("export")
        if export_format:
            columns = [
                "full_name",
                "on_time_count",
                "late_count",
                "absence_justified_count",
                "absence_unjustified_count",
                "total_worked_hours",
            ]
            return ReportExportService.export_queryset(
                rows=rows,
                columns=columns,
                format=export_format,
                filename=f"asistencia_{date_from}_a_{date_to}",
            )
        return Response(rows)


class PayrollCostReportView(APIView):
    """GET /usuarios/reports/payroll-cost/?period_start=&period_end=&export=
    (Sprint 23). Costo total de planilla en un rango de periodos."""

    permission_classes = _HR_PERMISSIONS

    def get(self, request):
        period_start = request.query_params.get("period_start")
        period_end = request.query_params.get("period_end")
        if not period_start or not period_end:
            raise ValidationError("period_start y period_end son requeridos.")

        queryset = (
            EmployeePayroll.objects.select_related("employee")
            .filter(period_start__gte=period_start, period_end__lte=period_end)
            .order_by("employee__full_name")
        )

        rows = [
            {
                "employee_id": payroll.employee_id,
                "full_name": payroll.employee.full_name,
                "period_start": str(payroll.period_start),
                "period_end": str(payroll.period_end),
                "base_salary": str(payroll.base_salary),
                "bonuses": str(payroll.bonuses),
                "deductions": str(payroll.deductions),
                "net_amount": str(payroll.net_amount),
                "status": payroll.status,
            }
            for payroll in queryset
        ]

        export_format = request.query_params.get("export")
        if export_format:
            columns = [
                "full_name",
                "period_start",
                "period_end",
                "base_salary",
                "bonuses",
                "deductions",
                "net_amount",
                "status",
            ]
            return ReportExportService.export_queryset(
                rows=rows,
                columns=columns,
                format=export_format,
                filename=f"costo_planilla_{period_start}_a_{period_end}",
            )
        return Response(
            {
                "total_net_amount": str(
                    sum((payroll.net_amount for payroll in queryset), Decimal("0"))
                ),
                "rows": rows,
            }
        )
