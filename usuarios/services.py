import csv
import io
import json
from datetime import timedelta
from decimal import Decimal

from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.http import HttpResponse
from django.utils import timezone
from rest_framework.exceptions import APIException

from usuarios.models import (
    AuditLog,
    Employee,
    EmployeeAttendance,
    EmployeePayroll,
    EmployeeSchedule,
    PasswordResetToken,
    Permission,
    Role,
    RolePermission,
    RolePermissionsHistory,
    User,
    UserPermission,
)

_PERMISSION_CACHE_TTL = 300
_PERMISSION_CACHE_PREFIX = "usuarios:permissions"
_RESET_TOKEN_TTL_MINUTES = 30


class PermissionService:
    """Unico punto de verdad para saber que puede hacer un usuario -combina
    el permiso heredado del rol con los overrides individuales de
    UserPermission (Esquema Backend §4.2). HasPermission (permissions.py) es
    la unica clase de permisos de DRF que debe consultar este servicio.
    """

    @staticmethod
    def _cache_key(user_id: int) -> str:
        # usuarios.User.id NO es globalmente unico -cada esquema de tenant
        # tiene su propia secuencia autoincremental, asi que el usuario #1
        # existe en practicamente todos los tenants. Sin el schema_name en
        # la key, el cache de permisos de un tenant se filtraba al usuario
        # con el mismo id de OTRO tenant (encontrado via las pruebas de
        # impersonacion del Sprint 10, que crean muchos tenants nuevos -cada
        # uno con su propio usuario #1- en la misma corrida).
        return f"{_PERMISSION_CACHE_PREFIX}:{connection.schema_name}:{user_id}"

    @staticmethod
    def _resolve_codes(user) -> set[str]:
        role_codes = set(
            Permission.objects.filter(
                role_permissions__role_id=user.role_id
            ).values_list("code", flat=True)
        )
        overrides = UserPermission.objects.filter(user=user).select_related(
            "permission"
        )
        for override in overrides:
            if override.is_granted:
                role_codes.add(override.permission.code)
            else:
                role_codes.discard(override.permission.code)
        return role_codes

    @staticmethod
    def get_permission_codes(user) -> set[str]:
        key = PermissionService._cache_key(user.id)
        cached = cache.get(key)
        if cached is not None:
            return cached
        codes = PermissionService._resolve_codes(user)
        cache.set(key, codes, _PERMISSION_CACHE_TTL)
        return codes

    @staticmethod
    def check_permission(user, permission_code: str) -> bool:
        return permission_code in PermissionService.get_permission_codes(user)

    @staticmethod
    def invalidate_user_cache(user_id: int) -> None:
        cache.delete(PermissionService._cache_key(user_id))

    @staticmethod
    def invalidate_role_cache(role_id: int) -> None:
        """Invalida a TODOS los usuarios de un rol -se usa cuando cambia el
        conjunto de permisos del rol, no de un usuario individual."""
        from usuarios.models import User

        user_ids = User.objects.filter(role_id=role_id).values_list("id", flat=True)
        for user_id in user_ids:
            PermissionService.invalidate_user_cache(user_id)


class CannotDeleteSystemRoleError(APIException):
    status_code = 409
    default_code = "CANNOT_DELETE_SYSTEM_ROLE"
    default_detail = {
        "error": {
            "code": "CANNOT_DELETE_SYSTEM_ROLE",
            "message": "admin, manager y seller son roles del sistema y no se pueden eliminar.",
        }
    }


class RoleInUseError(APIException):
    status_code = 409
    default_code = "ROLE_IN_USE"
    default_detail = {
        "error": {
            "code": "ROLE_IN_USE",
            "message": "Todavía hay usuarios con este rol. Reasígnalos a otro rol antes de eliminarlo.",
        }
    }


class RoleService:
    """Gestiona la asignacion de permisos a un rol, dejando siempre un
    registro en RolePermissionsHistory -nunca se edita role_permissions
    directamente sin pasar por aqui (Esquema Backend §4.2)."""

    @staticmethod
    def delete_role(role: Role, deleted_by) -> None:
        """Baja logica, nunca DELETE fisico -ver docstring de Role
        (SoftDeleteModel) sobre por que un hard delete rompe apenas el rol
        tiene algun permiso concedido/revocado alguna vez."""
        if role.is_system_default:
            raise CannotDeleteSystemRoleError()
        # role.users usa el manager por defecto de User (ActiveManager),
        # que ya excluye usuarios dados de baja -no hace falta filtrar
        # deleted_at a mano aqui.
        if role.users.exists():
            raise RoleInUseError()

        role.deleted_at = timezone.now()
        role.deleted_by = deleted_by
        role.save(update_fields=["deleted_at", "deleted_by"])

    @staticmethod
    def grant_permission(
        role: Role, permission: Permission, changed_by
    ) -> RolePermission:
        role_permission, created = RolePermission.objects.get_or_create(
            role=role, permission=permission
        )
        if created:
            RolePermissionsHistory.objects.create(
                role=role,
                permission=permission,
                action="GRANTED",
                changed_by=changed_by,
            )
            PermissionService.invalidate_role_cache(role.id)
        return role_permission

    @staticmethod
    def revoke_permission(role: Role, permission: Permission, changed_by) -> None:
        deleted, _ = RolePermission.objects.filter(
            role=role, permission=permission
        ).delete()
        if deleted:
            RolePermissionsHistory.objects.create(
                role=role,
                permission=permission,
                action="REVOKED",
                changed_by=changed_by,
            )
            PermissionService.invalidate_role_cache(role.id)


class AuditLogService:
    """Unico punto de entrada para escribir en tenant.audit_logs (Esquema
    Backend §4.2, §4.3). Ninguna app de negocio inserta en AuditLog
    directamente -cada servicio que ejecuta una accion critica llama a
    log_action() explicitamente, nunca via señal generica."""

    @staticmethod
    def log_action(
        user,
        action: str,
        entity: str,
        entity_id: int,
        details: str | dict | None = None,
    ) -> AuditLog:
        if isinstance(details, dict):
            details = json.dumps(details, default=str)

        # Sprint 10 (Especificacion de API §4.24): si esta accion ocurrio
        # bajo una sesion de impersonacion, se marca explicitamente aqui -sin
        # que cada call site (repartido en las 4 apps de negocio) tenga que
        # saber nada de impersonacion. TenantValidatedJWTAuthentication fija
        # este contexto por request.
        from core.impersonation_context import get_impersonating_staff

        staff_id = get_impersonating_staff()
        if staff_id is not None:
            marker = f"[accion de soporte Fivuza, platform_staff #{staff_id}] "
            details = marker + (details or "")

        return AuditLog.objects.create(
            user=user,
            action=action,
            entity=entity,
            entity_id=entity_id,
            details=details or "",
        )


class OpenAttendanceExistsError(APIException):
    """Sprint 22: un trabajador no puede marcar entrada dos veces sin haber
    marcado salida de la primera -evita que dos dispositivos (o el mismo,
    con doble clic) le abran dos jornadas simultaneas."""

    status_code = 409
    default_code = "OPEN_ATTENDANCE_EXISTS"
    default_detail = {
        "error": {
            "code": "OPEN_ATTENDANCE_EXISTS",
            "message": "Este trabajador ya tiene una entrada marcada sin salida registrada.",
        }
    }


class AttendanceAlreadyClosedError(APIException):
    status_code = 409
    default_code = "ATTENDANCE_ALREADY_CLOSED"
    default_detail = {
        "error": {
            "code": "ATTENDANCE_ALREADY_CLOSED",
            "message": "Esta marcacion ya tiene una salida registrada.",
        }
    }


_DAY_OF_WEEK_BY_PYTHON_WEEKDAY = [
    "MONDAY",
    "TUESDAY",
    "WEDNESDAY",
    "THURSDAY",
    "FRIDAY",
    "SATURDAY",
    "SUNDAY",
]


class AttendanceService:
    """Marcado real de entrada/salida (Esquema Backend §4.2). El horario
    programado (EmployeeSchedule) es la referencia contra la que se evalua
    si una entrada llego a tiempo -no existe un umbral de tolerancia
    documentado en ningun otro sitio, asi que se asume 0 minutos de gracia:
    cualquier check_in posterior a start_time cuenta como LATE. Si el
    trabajador no tiene un horario activo para ese dia de la semana, no hay
    contra que comparar -se registra ON_TIME por defecto en vez de
    penalizar una ausencia de horario."""

    @staticmethod
    def clock_in(*, employee: Employee, warehouse, user) -> EmployeeAttendance:
        if EmployeeAttendance.objects.filter(
            employee=employee, check_out__isnull=True
        ).exists():
            raise OpenAttendanceExistsError()

        check_in_at = timezone.now()
        status = AttendanceService._determine_status(employee, check_in_at)

        attendance = EmployeeAttendance.objects.create(
            employee=employee,
            warehouse=warehouse,
            check_in=check_in_at,
            status=status,
        )
        AuditLogService.log_action(
            user=user,
            action="EMPLOYEE_CLOCKED_IN",
            entity="EmployeeAttendance",
            entity_id=attendance.id,
            details={"employee_id": employee.id, "status": status},
        )
        return attendance

    @staticmethod
    def clock_out(*, attendance: EmployeeAttendance, user) -> EmployeeAttendance:
        if attendance.check_out is not None:
            raise AttendanceAlreadyClosedError()

        attendance.check_out = timezone.now()
        attendance.save(update_fields=["check_out"])
        AuditLogService.log_action(
            user=user,
            action="EMPLOYEE_CLOCKED_OUT",
            entity="EmployeeAttendance",
            entity_id=attendance.id,
            details={"employee_id": attendance.employee_id},
        )
        return attendance

    @staticmethod
    def _determine_status(employee: Employee, check_in_at) -> str:
        day_of_week = _DAY_OF_WEEK_BY_PYTHON_WEEKDAY[check_in_at.weekday()]
        schedule = EmployeeSchedule.objects.filter(
            employee=employee, day_of_week=day_of_week, is_active=True
        ).first()
        if schedule is None:
            return "ON_TIME"
        return "LATE" if check_in_at.time() > schedule.start_time else "ON_TIME"

    @staticmethod
    def calculate_worked_hours(attendance: EmployeeAttendance) -> Decimal | None:
        # check_in/check_out son datetime absolutos, no solo horas -restarlos
        # da el resultado correcto incluso si el turno cruza medianoche
        # (ej. entra 22:00, sale 06:00 del dia siguiente = 8 horas), sin
        # necesitar ningun caso especial para el cruce.
        if attendance.check_out is None:
            return None
        delta = attendance.check_out - attendance.check_in
        return (Decimal(delta.total_seconds()) / Decimal(3600)).quantize(
            Decimal("0.01")
        )


class PayrollAlreadyExistsError(APIException):
    """Sprint 23: los montos de una planilla se **congelan** al generarla
    -no existe un endpoint de recalculo. Si ya existe una planilla para ese
    trabajador+periodo, generar de nuevo no la reemplaza (perderia el
    congelamiento); el administrador debe corregir los bonos/descuentos de
    la existente si todavia esta PENDING, o vivir con lo ya pagado si esta
    PAID -mismo criterio que "un pago confirmado no se edita" del resto del
    sistema (ej. CashSession cerrada)."""

    status_code = 409
    default_code = "PAYROLL_ALREADY_EXISTS"
    default_detail = {
        "error": {
            "code": "PAYROLL_ALREADY_EXISTS",
            "message": "Ya existe una planilla generada para este trabajador en este periodo.",
        }
    }


class PayrollAlreadyPaidError(APIException):
    status_code = 409
    default_code = "PAYROLL_ALREADY_PAID"
    default_detail = {
        "error": {
            "code": "PAYROLL_ALREADY_PAID",
            "message": "Esta planilla ya esta marcada como pagada.",
        }
    }


class PayrollService:
    """Generacion de planilla por periodo (Sprint 23, Esquema Backend
    §4.2). El sueldo base se calcula segun salary_type -MONTHLY no depende
    de la asistencia real (el trabajador cobra su sueldo fijo salvo que el
    administrador lo ajuste a mano vía descuentos), DAILY/HOURLY si, contra
    EmployeeAttendance del periodo. bonuses/deductions son siempre
    ingresados a mano por el administrador -no hay ninguna regla automatica
    documentada para generarlos."""

    @staticmethod
    def generate_payroll(
        *,
        employee: Employee,
        period_start,
        period_end,
        bonuses: Decimal = Decimal("0"),
        deductions: Decimal = Decimal("0"),
        user,
    ) -> EmployeePayroll:
        if EmployeePayroll.objects.filter(
            employee=employee, period_start=period_start, period_end=period_end
        ).exists():
            raise PayrollAlreadyExistsError()

        base_salary = PayrollService._calculate_base_salary(
            employee, period_start, period_end
        )
        net_amount = base_salary + bonuses - deductions

        payroll = EmployeePayroll.objects.create(
            employee=employee,
            period_start=period_start,
            period_end=period_end,
            base_salary=base_salary,
            bonuses=bonuses,
            deductions=deductions,
            net_amount=net_amount,
            status="PENDING",
        )
        AuditLogService.log_action(
            user=user,
            action="PAYROLL_GENERATED",
            entity="EmployeePayroll",
            entity_id=payroll.id,
            details={
                "employee_id": employee.id,
                "period_start": str(period_start),
                "period_end": str(period_end),
                "net_amount": str(net_amount),
            },
        )
        return payroll

    @staticmethod
    def _calculate_base_salary(employee: Employee, period_start, period_end) -> Decimal:
        if employee.salary_type == "MONTHLY":
            return employee.salary_amount

        attendance = EmployeeAttendance.objects.filter(
            employee=employee,
            check_in__date__gte=period_start,
            check_in__date__lte=period_end,
            check_out__isnull=False,
        )

        # Los montos de dinero en todo el sistema usan 4 decimales
        # (DecimalField(max_digits=12, decimal_places=4), igual que
        # Sale.total) -no 2, para no perder precision al multiplicar por
        # horas fraccionarias.
        if employee.salary_type == "DAILY":
            days_worked = len({entry.check_in.date() for entry in attendance})
            return (employee.salary_amount * days_worked).quantize(Decimal("0.0001"))

        # HOURLY
        total_hours = sum(
            (AttendanceService.calculate_worked_hours(entry) for entry in attendance),
            Decimal("0"),
        )
        return (employee.salary_amount * total_hours).quantize(Decimal("0.0001"))

    @staticmethod
    def mark_paid(*, payroll: EmployeePayroll, payment_date, user) -> EmployeePayroll:
        if payroll.status == "PAID":
            raise PayrollAlreadyPaidError()

        payroll.status = "PAID"
        payroll.payment_date = payment_date
        payroll.save(update_fields=["status", "payment_date"])
        AuditLogService.log_action(
            user=user,
            action="PAYROLL_PAID",
            entity="EmployeePayroll",
            entity_id=payroll.id,
            details={
                "employee_id": payroll.employee_id,
                "net_amount": str(payroll.net_amount),
            },
        )
        return payroll


class ReportExportService:
    """Motor generico de exportacion de reportes a CSV/XLSX (Sprint 23,
    Esquema Backend §4.2; API Spec §4.16). Recibe las mismas filas que el
    reporte en pantalla ya construyo -exportar y ver en pantalla nunca
    pueden divergir, porque ambos parten de la misma consulta. Vive en
    usuarios por ser transversal (lo consumen los reportes de todas las
    apps de negocio), no porque sea especifico de RRHH.

    [ALCANCE] Esta version cubre solo el camino sincronico. El encolado en
    Celery + S3 para reportes de mas de 5000 filas que documenta la API
    Spec §4.16 NO esta implementado -no hay infraestructura de S3 real
    contra la que probarlo en este entorno de desarrollo; queda como hueco
    documentado, igual que la prueba de impresora termica del Sprint 17."""

    XLSX_ROW_LIMIT = 1_048_576

    @staticmethod
    def export_queryset(
        *, rows: list[dict], columns: list[str], format: str, filename: str
    ) -> HttpResponse:
        if format not in ("csv", "xlsx"):
            raise ValueError(f"Formato de exportacion no soportado: {format}")

        # Una hoja XLSX no admite mas de XLSX_ROW_LIMIT filas -forzar CSV en
        # vez de intentar escribirla produce un archivo descargable en vez
        # de uno corrupto (API Spec §4.16: "superar el limite produce un
        # archivo corrupto, no un error claro").
        if format == "xlsx" and len(rows) > ReportExportService.XLSX_ROW_LIMIT:
            format = "csv"

        if format == "csv":
            return ReportExportService._to_csv(rows, columns, filename)
        return ReportExportService._to_xlsx(rows, columns, filename)

    @staticmethod
    def _to_csv(rows: list[dict], columns: list[str], filename: str) -> HttpResponse:
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)
        response = HttpResponse(buffer.getvalue(), content_type="text/csv")
        response["Content-Disposition"] = f'attachment; filename="{filename}.csv"'
        return response

    @staticmethod
    def _to_xlsx(rows: list[dict], columns: list[str], filename: str) -> HttpResponse:
        from openpyxl import Workbook

        workbook = Workbook()
        sheet = workbook.active
        sheet.append(columns)
        for row in rows:
            sheet.append([row.get(column, "") for column in columns])

        buffer = io.BytesIO()
        workbook.save(buffer)
        response = HttpResponse(
            buffer.getvalue(),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        response["Content-Disposition"] = f'attachment; filename="{filename}.xlsx"'
        return response


class PasswordResetService:
    """Flujo de 'olvide mi contraseña' (Sprint 5, hueco #1). request_reset()
    nunca revela si el correo existe -siempre se comporta igual desde
    afuera, para no filtrar que correos estan registrados."""

    @staticmethod
    def request_reset(*, email: str, schema_name: str, frontend_origin: str) -> None:
        try:
            user = User.objects.get(email=email, is_active=True)
        except User.DoesNotExist:
            return

        token = PasswordResetToken.objects.create(
            user=user,
            expires_at=timezone.now() + timedelta(minutes=_RESET_TOKEN_TTL_MINUTES),
        )

        from usuarios.tasks import send_password_reset_email

        send_password_reset_email.delay(
            user_id=user.id,
            token=token.token,
            schema_name=schema_name,
            frontend_origin=frontend_origin,
        )

    @staticmethod
    @transaction.atomic
    def confirm_reset(*, token: str, new_password: str) -> User:
        try:
            reset_token = PasswordResetToken.objects.select_for_update().get(
                token=token
            )
        except PasswordResetToken.DoesNotExist:
            raise ValidationError("El enlace de recuperacion es invalido o ya expiro.")

        if reset_token.used_at is not None or reset_token.expires_at < timezone.now():
            raise ValidationError("El enlace de recuperacion es invalido o ya expiro.")

        user = reset_token.user
        user.set_password(new_password)
        user.save(update_fields=["password"])

        reset_token.used_at = timezone.now()
        reset_token.save(update_fields=["used_at"])
        return user
