import json
from datetime import timedelta
from decimal import Decimal

from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.utils import timezone
from rest_framework.exceptions import APIException

from usuarios.models import (
    AuditLog,
    Employee,
    EmployeeAttendance,
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
