# Pruebas de modelos: validaciones de campo, constraints, métodos del modelo.
from datetime import date, datetime, timedelta
from datetime import timezone as dt_timezone
from decimal import Decimal
from unittest.mock import patch

from django.core import mail
from django.core.exceptions import ValidationError
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase
from rest_framework.exceptions import APIException

from core.models import TenantSettings
from inventario.models import Warehouse
from usuarios.models import (
    AuditLog,
    Employee,
    EmployeeAttendance,
    EmployeeSchedule,
    PasswordResetToken,
    Permission,
    Role,
    RolePermission,
    User,
    UserPermission,
)
from usuarios.services import (
    AttendanceService,
    AuditLogService,
    PasswordResetService,
    PayrollAlreadyExistsError,
    PayrollAlreadyPaidError,
    PayrollService,
    PermissionService,
    RoleService,
)


class PermissionServiceTests(TenantTestCase):
    @classmethod
    def get_test_schema_name(cls):
        return "test_permission_service"

    @classmethod
    def get_test_tenant_domain(cls):
        return "test-permission-service.test.com"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.role = Role.objects.create(name="seller")
        cls.perm_sell = Permission.objects.create(code="SALES_CREATE", module="SALES")
        cls.perm_view = Permission.objects.create(code="SALES_VIEW", module="SALES")
        RolePermission.objects.create(role=cls.role, permission=cls.perm_sell)
        cls.user = User.objects.create(email="vendedor@negocio.com", role=cls.role)

    @classmethod
    def tearDownClass(cls):
        TenantSettings.objects.filter(tenant=cls.tenant).delete()
        super().tearDownClass()

    def setUp(self):
        # La cache de permisos vive en Redis, fuera de la transaccion de
        # base de datos que Django revierte al terminar cada test -sin este
        # limpiado, un valor cacheado por un test anterior (calculado sobre
        # un estado de BD que luego se revirtio) contaminaria este test.
        from django.core.cache import cache

        cache.clear()

    def test_user_has_permission_inherited_from_role(self):
        self.assertTrue(PermissionService.check_permission(self.user, "SALES_CREATE"))
        self.assertFalse(PermissionService.check_permission(self.user, "SALES_VIEW"))

    def test_individual_override_grants_extra_permission(self):
        UserPermission.objects.create(
            user=self.user, permission=self.perm_view, is_granted=True
        )
        PermissionService.invalidate_user_cache(self.user.id)
        self.assertTrue(PermissionService.check_permission(self.user, "SALES_VIEW"))

    def test_individual_override_revokes_role_permission(self):
        UserPermission.objects.create(
            user=self.user, permission=self.perm_sell, is_granted=False
        )
        PermissionService.invalidate_user_cache(self.user.id)
        self.assertFalse(PermissionService.check_permission(self.user, "SALES_CREATE"))

    def test_result_is_cached_until_invalidated(self):
        self.assertTrue(PermissionService.check_permission(self.user, "SALES_CREATE"))

        # Revocar el permiso del rol directamente (sin pasar por RoleService)
        # no deberia reflejarse hasta invalidar la cache manualmente -es
        # exactamente el comportamiento de cache que se esta probando.
        RolePermission.objects.filter(
            role=self.role, permission=self.perm_sell
        ).delete()
        self.assertTrue(PermissionService.check_permission(self.user, "SALES_CREATE"))

        PermissionService.invalidate_user_cache(self.user.id)
        self.assertFalse(PermissionService.check_permission(self.user, "SALES_CREATE"))

    def test_invalidate_role_cache_affects_all_users_of_that_role(self):
        other_user = User.objects.create(email="otro@negocio.com", role=self.role)
        self.assertTrue(PermissionService.check_permission(other_user, "SALES_CREATE"))

        RolePermission.objects.filter(
            role=self.role, permission=self.perm_sell
        ).delete()
        PermissionService.invalidate_role_cache(self.role.id)

        self.assertFalse(PermissionService.check_permission(self.user, "SALES_CREATE"))
        self.assertFalse(PermissionService.check_permission(other_user, "SALES_CREATE"))


class RoleServiceTests(TenantTestCase):
    @classmethod
    def get_test_schema_name(cls):
        return "test_role_service"

    @classmethod
    def get_test_tenant_domain(cls):
        return "test-role-service.test.com"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Rol y permiso propios del test, con un code que no colisiona con
        # el catalogo base que TenantProvisioningService.seed_default_roles()
        # ya sembro al crear el tenant (post_schema_sync).
        cls.role = Role.objects.create(name="rol_de_prueba")
        cls.permission = Permission.objects.create(
            code="TEST_ROLE_SERVICE_PERM", module="HR"
        )
        cls.admin = User.objects.create(email="admin@negocio.com", role=cls.role)

    @classmethod
    def tearDownClass(cls):
        TenantSettings.objects.filter(tenant=cls.tenant).delete()
        super().tearDownClass()

    def test_grant_permission_creates_role_permission_and_history(self):
        from usuarios.models import RolePermissionsHistory

        RoleService.grant_permission(self.role, self.permission, changed_by=self.admin)

        self.assertTrue(
            RolePermission.objects.filter(
                role=self.role, permission=self.permission
            ).exists()
        )
        self.assertTrue(
            RolePermissionsHistory.objects.filter(
                role=self.role, permission=self.permission, action="GRANTED"
            ).exists()
        )

    def test_granting_same_permission_twice_writes_history_only_once(self):
        from usuarios.models import RolePermissionsHistory

        RoleService.grant_permission(self.role, self.permission, changed_by=self.admin)
        RoleService.grant_permission(self.role, self.permission, changed_by=self.admin)

        self.assertEqual(
            RolePermissionsHistory.objects.filter(
                role=self.role, permission=self.permission, action="GRANTED"
            ).count(),
            1,
        )

    def test_revoke_permission_removes_row_and_writes_history(self):
        from usuarios.models import RolePermissionsHistory

        RoleService.grant_permission(self.role, self.permission, changed_by=self.admin)
        RoleService.revoke_permission(self.role, self.permission, changed_by=self.admin)

        self.assertFalse(
            RolePermission.objects.filter(
                role=self.role, permission=self.permission
            ).exists()
        )
        self.assertTrue(
            RolePermissionsHistory.objects.filter(
                role=self.role, permission=self.permission, action="REVOKED"
            ).exists()
        )


class AuditLogServiceTests(TenantTestCase):
    @classmethod
    def get_test_schema_name(cls):
        return "test_audit_log_service"

    @classmethod
    def get_test_tenant_domain(cls):
        return "test-audit-log-service.test.com"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.role = Role.objects.create(name="admin")
        cls.user = User.objects.create(email="admin@negocio.com", role=cls.role)

    @classmethod
    def tearDownClass(cls):
        TenantSettings.objects.filter(tenant=cls.tenant).delete()
        super().tearDownClass()

    def test_log_action_persists_entry(self):
        AuditLogService.log_action(
            user=self.user,
            action="USER_ROLE_CHANGED",
            entity="Role",
            entity_id=self.role.id,
            details={"granted": "HR_MANAGE"},
        )

        entry = AuditLog.objects.get(user=self.user, action="USER_ROLE_CHANGED")
        self.assertEqual(entry.entity, "Role")
        self.assertIn("HR_MANAGE", entry.details)

    def test_log_action_accepts_plain_string_details(self):
        AuditLogService.log_action(
            user=self.user,
            action="USER_ROLE_CHANGED",
            entity="Role",
            entity_id=self.role.id,
            details="detalle en texto plano",
        )
        entry = AuditLog.objects.get(user=self.user, action="USER_ROLE_CHANGED")
        self.assertEqual(entry.details, "detalle en texto plano")


class PasswordResetServiceTests(TenantTestCase):
    """Flujo de 'olvide mi contraseña' (Sprint 5, hueco #1)."""

    @classmethod
    def get_test_schema_name(cls):
        return "test_usuarios_password_reset"

    @classmethod
    def get_test_tenant_domain(cls):
        return "test-usuarios-password-reset.test.com"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        role = Role.objects.create(name="admin", is_system_default=True)
        cls.user = User.objects.create(email="admin@negocio.com", role=role)
        cls.user.set_password("ClaveVieja123")
        cls.user.save()

    @classmethod
    def tearDownClass(cls):
        TenantSettings.objects.filter(tenant=cls.tenant).delete()
        super().tearDownClass()

    def test_request_reset_for_existing_email_sends_email(self):
        PasswordResetService.request_reset(
            email=self.user.email,
            schema_name=self.tenant.schema_name,
            frontend_origin="http://tenant1.localhost:5173",
        )
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn(self.user.email, mail.outbox[0].to)
        self.assertTrue(PasswordResetToken.objects.filter(user=self.user).exists())

    def test_request_reset_for_unknown_email_sends_nothing(self):
        PasswordResetService.request_reset(
            email="no-existe@negocio.com",
            schema_name=self.tenant.schema_name,
            frontend_origin="http://tenant1.localhost:5173",
        )
        self.assertEqual(len(mail.outbox), 0)

    def test_confirm_reset_changes_password_and_consumes_token(self):
        token = PasswordResetToken.objects.create(
            user=self.user, expires_at=timezone.now() + timedelta(minutes=30)
        )
        PasswordResetService.confirm_reset(
            token=token.token, new_password="ClaveNueva456"
        )

        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("ClaveNueva456"))

        token.refresh_from_db()
        self.assertIsNotNone(token.used_at)

    def test_confirm_reset_rejects_expired_token(self):
        token = PasswordResetToken.objects.create(
            user=self.user, expires_at=timezone.now() - timedelta(minutes=1)
        )
        with self.assertRaises(ValidationError):
            PasswordResetService.confirm_reset(
                token=token.token, new_password="ClaveNueva456"
            )

    def test_confirm_reset_rejects_already_used_token(self):
        token = PasswordResetToken.objects.create(
            user=self.user,
            expires_at=timezone.now() + timedelta(minutes=30),
            used_at=timezone.now(),
        )
        with self.assertRaises(ValidationError):
            PasswordResetService.confirm_reset(
                token=token.token, new_password="ClaveNueva456"
            )

    def test_confirm_reset_rejects_unknown_token(self):
        with self.assertRaises(ValidationError):
            PasswordResetService.confirm_reset(
                token="token-inexistente", new_password="ClaveNueva456"
            )


class AttendanceServiceTests(TenantTestCase):
    """clock_in/clock_out y calculo de horas trabajadas (Sprint 22, Esquema
    Backend §4.2). Se fija timezone.now() con mock.patch para poder probar
    ON_TIME/LATE de forma determinista contra un horario conocido -sin esto,
    el resultado dependeria de la hora real en la que corre la suite."""

    @classmethod
    def get_test_schema_name(cls):
        return "test_attendance_service"

    @classmethod
    def get_test_tenant_domain(cls):
        return "test-attendance-service.test.com"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        role = Role.objects.create(name="admin", is_system_default=True)
        cls.user = User.objects.create(email="admin@negocio.com", role=role)
        cls.warehouse = Warehouse.objects.create(name="Principal")

    @classmethod
    def tearDownClass(cls):
        TenantSettings.objects.filter(tenant=cls.tenant).delete()
        super().tearDownClass()

    def setUp(self):
        self.employee = Employee.objects.create(
            full_name="Juan Perez",
            document_number="45678912",
            position="Almacenero",
            warehouse=self.warehouse,
            salary_type="MONTHLY",
            salary_amount="1500.00",
            hire_date=timezone.now().date(),
        )

    def _monday(self, hour, minute):
        # 2026-08-03 es lunes -fecha arbitraria, solo importa el dia de la semana.
        return datetime(2026, 8, 3, hour, minute, tzinfo=dt_timezone.utc)

    def test_clock_in_on_time_within_schedule(self):
        EmployeeSchedule.objects.create(
            employee=self.employee,
            day_of_week="MONDAY",
            start_time="09:00",
            end_time="17:00",
        )
        with patch("usuarios.services.timezone.now", return_value=self._monday(8, 55)):
            attendance = AttendanceService.clock_in(
                employee=self.employee, warehouse=self.warehouse, user=self.user
            )
        self.assertEqual(attendance.status, "ON_TIME")

    def test_clock_in_late_after_schedule_start(self):
        EmployeeSchedule.objects.create(
            employee=self.employee,
            day_of_week="MONDAY",
            start_time="09:00",
            end_time="17:00",
        )
        with patch("usuarios.services.timezone.now", return_value=self._monday(9, 5)):
            attendance = AttendanceService.clock_in(
                employee=self.employee, warehouse=self.warehouse, user=self.user
            )
        self.assertEqual(attendance.status, "LATE")

    def test_clock_in_without_active_schedule_defaults_to_on_time(self):
        # Sin EmployeeSchedule activo para ese dia, no hay contra que
        # comparar -no se penaliza al trabajador por la ausencia de horario.
        with patch("usuarios.services.timezone.now", return_value=self._monday(23, 0)):
            attendance = AttendanceService.clock_in(
                employee=self.employee, warehouse=self.warehouse, user=self.user
            )
        self.assertEqual(attendance.status, "ON_TIME")

    def test_clock_in_twice_without_clock_out_raises(self):
        AttendanceService.clock_in(
            employee=self.employee, warehouse=self.warehouse, user=self.user
        )
        with self.assertRaises(APIException) as ctx:
            AttendanceService.clock_in(
                employee=self.employee, warehouse=self.warehouse, user=self.user
            )
        self.assertEqual(ctx.exception.status_code, 409)

    def test_clock_out_closes_open_attendance(self):
        attendance = AttendanceService.clock_in(
            employee=self.employee, warehouse=self.warehouse, user=self.user
        )
        closed = AttendanceService.clock_out(attendance=attendance, user=self.user)
        self.assertIsNotNone(closed.check_out)

    def test_clock_out_already_closed_raises(self):
        attendance = AttendanceService.clock_in(
            employee=self.employee, warehouse=self.warehouse, user=self.user
        )
        AttendanceService.clock_out(attendance=attendance, user=self.user)
        with self.assertRaises(APIException) as ctx:
            AttendanceService.clock_out(attendance=attendance, user=self.user)
        self.assertEqual(ctx.exception.status_code, 409)

    def test_worked_hours_across_midnight_night_shift(self):
        # Entra 22:00 lunes, sale 06:00 martes -turno nocturno que cruza
        # medianoche (Sprint 22, Definicion de Hecho).
        check_in_at = self._monday(22, 0)
        check_out_at = datetime(2026, 8, 4, 6, 0, tzinfo=dt_timezone.utc)
        with patch("usuarios.services.timezone.now", return_value=check_in_at):
            attendance = AttendanceService.clock_in(
                employee=self.employee, warehouse=self.warehouse, user=self.user
            )
        with patch("usuarios.services.timezone.now", return_value=check_out_at):
            attendance = AttendanceService.clock_out(
                attendance=attendance, user=self.user
            )
        self.assertEqual(
            AttendanceService.calculate_worked_hours(attendance), Decimal("8.00")
        )

    def test_worked_hours_is_none_while_still_open(self):
        attendance = AttendanceService.clock_in(
            employee=self.employee, warehouse=self.warehouse, user=self.user
        )
        self.assertIsNone(AttendanceService.calculate_worked_hours(attendance))


class PayrollServiceTests(TenantTestCase):
    """generate_payroll()/mark_paid() (Sprint 23, Esquema Backend §4.2): el
    calculo del sueldo base depende de salary_type, y los montos se
    congelan al generarse -no hay ningun endpoint de recalculo."""

    @classmethod
    def get_test_schema_name(cls):
        return "test_payroll_service"

    @classmethod
    def get_test_tenant_domain(cls):
        return "test-payroll-service.test.com"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        role = Role.objects.create(name="admin", is_system_default=True)
        cls.user = User.objects.create(email="admin@negocio.com", role=role)
        cls.warehouse = Warehouse.objects.create(name="Principal")

    @classmethod
    def tearDownClass(cls):
        TenantSettings.objects.filter(tenant=cls.tenant).delete()
        super().tearDownClass()

    def _make_employee(self, salary_type, salary_amount, document_number):
        return Employee.objects.create(
            full_name="Juan Perez",
            document_number=document_number,
            position="Almacenero",
            warehouse=self.warehouse,
            salary_type=salary_type,
            # Decimal, no str -Employee.objects.create() no lo convierte
            # solo al asignarlo (encontrado al escribir este test: sumar un
            # str "1500.00" con un Decimal explota, silenciosamente NO se
            # castea al guardar el objeto en memoria como si fuera un campo
            # normal de formulario).
            salary_amount=Decimal(salary_amount),
            hire_date=timezone.now().date(),
        )

    def _make_closed_attendance(self, employee, check_in, check_out):
        return EmployeeAttendance.objects.create(
            employee=employee,
            warehouse=self.warehouse,
            check_in=check_in,
            check_out=check_out,
            status="ON_TIME",
        )

    def test_monthly_salary_ignores_attendance(self):
        employee = self._make_employee("MONTHLY", "1500.00", "10000001")
        payroll = PayrollService.generate_payroll(
            employee=employee,
            period_start=date(2026, 8, 1),
            period_end=date(2026, 8, 31),
            user=self.user,
        )
        self.assertEqual(payroll.base_salary, Decimal("1500.00"))
        self.assertEqual(payroll.net_amount, Decimal("1500.00"))

    def test_daily_salary_counts_distinct_worked_days(self):
        employee = self._make_employee("DAILY", "50.00", "10000002")
        self._make_closed_attendance(
            employee,
            datetime(2026, 8, 3, 9, 0, tzinfo=dt_timezone.utc),
            datetime(2026, 8, 3, 17, 0, tzinfo=dt_timezone.utc),
        )
        self._make_closed_attendance(
            employee,
            datetime(2026, 8, 4, 9, 0, tzinfo=dt_timezone.utc),
            datetime(2026, 8, 4, 17, 0, tzinfo=dt_timezone.utc),
        )
        payroll = PayrollService.generate_payroll(
            employee=employee,
            period_start=date(2026, 8, 1),
            period_end=date(2026, 8, 31),
            user=self.user,
        )
        self.assertEqual(payroll.base_salary, Decimal("100.00"))

    def test_hourly_salary_sums_worked_hours(self):
        employee = self._make_employee("HOURLY", "10.00", "10000003")
        self._make_closed_attendance(
            employee,
            datetime(2026, 8, 3, 22, 0, tzinfo=dt_timezone.utc),
            datetime(2026, 8, 4, 6, 0, tzinfo=dt_timezone.utc),
        )
        payroll = PayrollService.generate_payroll(
            employee=employee,
            period_start=date(2026, 8, 1),
            period_end=date(2026, 8, 31),
            user=self.user,
        )
        self.assertEqual(payroll.base_salary, Decimal("80.00"))

    def test_bonuses_and_deductions_affect_net_amount(self):
        employee = self._make_employee("MONTHLY", "1500.00", "10000004")
        payroll = PayrollService.generate_payroll(
            employee=employee,
            period_start=date(2026, 8, 1),
            period_end=date(2026, 8, 31),
            bonuses=Decimal("100.00"),
            deductions=Decimal("30.00"),
            user=self.user,
        )
        self.assertEqual(payroll.net_amount, Decimal("1570.00"))

    def test_generating_twice_for_same_period_raises(self):
        employee = self._make_employee("MONTHLY", "1500.00", "10000005")
        PayrollService.generate_payroll(
            employee=employee,
            period_start=date(2026, 8, 1),
            period_end=date(2026, 8, 31),
            user=self.user,
        )
        with self.assertRaises(PayrollAlreadyExistsError):
            PayrollService.generate_payroll(
                employee=employee,
                period_start=date(2026, 8, 1),
                period_end=date(2026, 8, 31),
                user=self.user,
            )

    def test_mark_paid_sets_status_and_payment_date(self):
        employee = self._make_employee("MONTHLY", "1500.00", "10000006")
        payroll = PayrollService.generate_payroll(
            employee=employee,
            period_start=date(2026, 8, 1),
            period_end=date(2026, 8, 31),
            user=self.user,
        )
        paid = PayrollService.mark_paid(
            payroll=payroll, payment_date=date(2026, 9, 1), user=self.user
        )
        self.assertEqual(paid.status, "PAID")
        self.assertEqual(paid.payment_date, date(2026, 9, 1))

    def test_mark_paid_twice_raises_and_does_not_alter_frozen_amount(self):
        employee = self._make_employee("MONTHLY", "1500.00", "10000007")
        payroll = PayrollService.generate_payroll(
            employee=employee,
            period_start=date(2026, 8, 1),
            period_end=date(2026, 8, 31),
            bonuses=Decimal("50.00"),
            user=self.user,
        )
        PayrollService.mark_paid(
            payroll=payroll, payment_date=date(2026, 9, 1), user=self.user
        )
        with self.assertRaises(PayrollAlreadyPaidError):
            PayrollService.mark_paid(
                payroll=payroll, payment_date=date(2026, 9, 2), user=self.user
            )

        payroll.refresh_from_db()
        self.assertEqual(payroll.net_amount, Decimal("1550.00"))
        self.assertEqual(payroll.payment_date, date(2026, 9, 1))
