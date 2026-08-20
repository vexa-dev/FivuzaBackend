# Pruebas de ViewSets/vistas: permisos, serialización, códigos de respuesta HTTP.
from unittest import mock

from django.core.cache import cache
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase
from rest_framework.test import APIClient

from core.models import TenantSettings
from core.throttling import LoginRateThrottle
from inventario.models import Warehouse
from usuarios.models import Permission, Role, User


class TenantUserAuthTests(TenantTestCase):
    """Login/refresh/logout de tenant.users (API Spec §3.1, Sprint 2)."""

    @classmethod
    def get_test_schema_name(cls):
        return "test_usuarios_auth"

    @classmethod
    def get_test_tenant_domain(cls):
        return "test-usuarios-auth.test.com"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.role = Role.objects.create(name="admin", is_system_default=True)
        cls.password = "ClaveSegura123"
        cls.user = User.objects.create(email="admin@negocio.com", role=cls.role)
        cls.user.set_password(cls.password)
        cls.user.save()

    @classmethod
    def tearDownClass(cls):
        TenantSettings.objects.filter(tenant=cls.tenant).delete()
        super().tearDownClass()

    def setUp(self):
        # Ver PermissionServiceTests.setUp (usuarios/tests/test_models.py):
        # la cache de permisos vive en Redis, fuera de la transaccion de BD
        # que Django revierte al terminar cada test.
        from django.core.cache import cache

        cache.clear()
        self.client = APIClient(HTTP_HOST=self.domain.domain)

    def _login(self):
        return self.client.post(
            "/api/v1/auth/login/",
            {"email": self.user.email, "password": self.password},
            format="json",
        )

    def test_login_with_valid_credentials_returns_tokens(self):
        response = self._login()
        self.assertEqual(response.status_code, 200)
        self.assertIn("access", response.data)
        self.assertNotIn("refresh", response.data)
        self.assertTrue(response.cookies["fivuza_tenant_refresh"]["httponly"])
        self.assertEqual(response.data["user"]["email"], self.user.email)

    def test_login_response_includes_permission_codes(self):
        from usuarios.models import RolePermission

        permission = Permission.objects.create(code="TEST_LOGIN_PERM", module="USERS")
        RolePermission.objects.create(role=self.role, permission=permission)

        response = self._login()
        self.assertIn("TEST_LOGIN_PERM", response.data["user"]["permissions"])

    def test_login_with_wrong_password_fails(self):
        response = self.client.post(
            "/api/v1/auth/login/",
            {"email": self.user.email, "password": "incorrecta"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)

    def test_login_with_inactive_user_fails(self):
        self.user.is_active = False
        self.user.save()
        response = self._login()
        self.assertEqual(response.status_code, 400)

    @mock.patch.dict(LoginRateThrottle.THROTTLE_RATES, {"login_ip": "10/min"})
    def test_login_is_rate_limited_after_repeated_attempts(self):
        """Sprint 33 (TRD §6.1, §7.2): sin esto, /auth/login/ es un vector
        trivial de fuerza bruta -DEFAULT_THROTTLE_RATES["login"] = 10/min.
        En `settings.py` ese rate se desactiva bajo "test" in sys.argv (para
        no romper el resto de la suite con logins acumulados). DRF fija
        `THROTTLE_RATES` como atributo de clase al importar el modulo, asi
        que @override_settings no lo actualiza -hay que parchear el dict
        directamente. Cache limpio para no arrastrar conteos de otros tests
        que ya pegaron a login."""
        cache.clear()
        for _ in range(10):
            self.client.post(
                "/api/v1/auth/login/",
                {"email": self.user.email, "password": "incorrecta"},
                format="json",
            )
        response = self.client.post(
            "/api/v1/auth/login/",
            {"email": self.user.email, "password": "incorrecta"},
            format="json",
        )
        self.assertEqual(response.status_code, 429)
        self.user.is_active = True
        self.user.save()

    def test_logout_is_idempotent_without_access_token(self):
        response = self.client.post("/api/v1/auth/logout/", {}, format="json")
        self.assertEqual(response.status_code, 205)

    def test_refresh_then_logout_blacklists_refresh_token(self):
        login = self._login()
        old_refresh = login.cookies["fivuza_tenant_refresh"].value

        refresh_response = self.client.post("/api/v1/auth/refresh/", {}, format="json")
        self.assertEqual(refresh_response.status_code, 200)
        self.assertNotIn("refresh", refresh_response.data)
        new_refresh = refresh_response.cookies["fivuza_tenant_refresh"].value

        old_client = APIClient(HTTP_HOST=self.domain.domain)
        old_client.cookies["fivuza_tenant_refresh"] = old_refresh
        old_refresh_reuse = old_client.post("/api/v1/auth/refresh/", {}, format="json")
        self.assertEqual(old_refresh_reuse.status_code, 401)

        logout_response = self.client.post("/api/v1/auth/logout/", {}, format="json")
        self.assertEqual(logout_response.status_code, 205)

        reuse_client = APIClient(HTTP_HOST=self.domain.domain)
        reuse_client.cookies["fivuza_tenant_refresh"] = new_refresh
        reuse_after_logout = reuse_client.post(
            "/api/v1/auth/refresh/", {}, format="json"
        )
        self.assertEqual(reuse_after_logout.status_code, 401)


class RoleRBACEndpointsTests(TenantTestCase):
    """CRUD de roles/permisos/usuarios y aplicacion de HasModulePermission
    (API Spec §2.1, Sprint 2)."""

    @classmethod
    def get_test_schema_name(cls):
        return "test_usuarios_rbac"

    @classmethod
    def get_test_tenant_domain(cls):
        return "test-usuarios-rbac.test.com"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # TenantProvisioningService.seed_default_roles() (post_schema_sync)
        # ya creo los roles admin/manager/seller y el catalogo base de
        # permisos al crear el tenant -se reutilizan aqui en vez de crear
        # duplicados con el mismo permissions.code (unique).
        cls.password = "ClaveSegura123"
        cls.admin_role = Role.objects.get(name="admin")
        cls.seller_role = Role.objects.get(name="seller")
        cls.manage_users_perm = Permission.objects.get(code="USERS_MANAGE")

        cls.admin_user = User.objects.create(
            email="admin@negocio.com", role=cls.admin_role
        )
        cls.admin_user.set_password(cls.password)
        cls.admin_user.save()

        cls.seller_user = User.objects.create(
            email="vendedor@negocio.com", role=cls.seller_role
        )
        cls.seller_user.set_password(cls.password)
        cls.seller_user.save()

    @classmethod
    def tearDownClass(cls):
        TenantSettings.objects.filter(tenant=cls.tenant).delete()
        super().tearDownClass()

    def setUp(self):
        # Ver PermissionServiceTests.setUp: la cache de permisos vive en
        # Redis, fuera de la transaccion de BD que Django revierte por test.
        from django.core.cache import cache

        cache.clear()

    def _client_as(self, user):
        client = APIClient(HTTP_HOST=self.domain.domain)
        login = client.post(
            "/api/v1/auth/login/",
            {"email": user.email, "password": self.password},
            format="json",
        )
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {login.data['access']}")
        return client

    def test_seller_without_permission_cannot_manage_users(self):
        response = self._client_as(self.seller_user).get("/api/v1/usuarios/users/")
        self.assertEqual(response.status_code, 403)

    def test_admin_with_permission_can_manage_users(self):
        response = self._client_as(self.admin_user).get("/api/v1/usuarios/users/")
        self.assertEqual(response.status_code, 200)

    def test_admin_can_create_user_with_hashed_password(self):
        response = self._client_as(self.admin_user).post(
            "/api/v1/usuarios/users/",
            {
                "email": "nuevo@negocio.com",
                "role": self.seller_role.id,
                "password": "OtraClave456",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        self.assertNotIn("password", response.data)

        created = User.objects.get(email="nuevo@negocio.com")
        self.assertNotEqual(created.password, "OtraClave456")
        self.assertTrue(created.check_password("OtraClave456"))

    def test_deleting_user_soft_deletes_and_excludes_from_default_manager(self):
        target = User.objects.create(email="baja@negocio.com", role=self.seller_role)
        response = self._client_as(self.admin_user).delete(
            f"/api/v1/usuarios/users/{target.id}/"
        )
        self.assertEqual(response.status_code, 204)

        self.assertFalse(User.objects.filter(id=target.id).exists())
        self.assertTrue(User.all_objects.filter(id=target.id).exists())

    def test_permissions_catalog_is_read_only(self):
        response = self._client_as(self.admin_user).post(
            "/api/v1/usuarios/permissions/",
            {"code": "FAKE_PERM", "module": "USERS"},
            format="json",
        )
        self.assertEqual(response.status_code, 405)

    def test_granting_role_permission_writes_history(self):
        from usuarios.models import RolePermissionsHistory

        response = self._client_as(self.admin_user).post(
            "/api/v1/usuarios/role-permissions/",
            {"role": self.seller_role.id, "permission": self.manage_users_perm.id},
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        self.assertTrue(
            RolePermissionsHistory.objects.filter(
                role=self.seller_role,
                permission=self.manage_users_perm,
                action="GRANTED",
            ).exists()
        )

    def test_admin_can_create_custom_role(self):
        response = self._client_as(self.admin_user).post(
            "/api/v1/usuarios/roles/",
            {"name": "Cajero", "description": "Atiende el mostrador"},
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        self.assertFalse(response.data["is_system_default"])

    def test_deleting_custom_role_soft_deletes(self):
        client = self._client_as(self.admin_user)
        created = client.post(
            "/api/v1/usuarios/roles/", {"name": "Limpieza"}, format="json"
        )
        role_id = created.data["id"]

        response = client.delete(f"/api/v1/usuarios/roles/{role_id}/")
        self.assertEqual(response.status_code, 204)
        self.assertFalse(Role.objects.filter(id=role_id).exists())
        self.assertTrue(Role.all_objects.filter(id=role_id).exists())

    def test_deleting_role_with_granted_permission_still_soft_deletes(self):
        # Este es el caso que rompia con un hard delete: RolePermissionsHistory
        # protege al rol apenas se le concede/revoca un permiso, que es el
        # primer paso natural despues de crear un rol a medida.
        client = self._client_as(self.admin_user)
        created = client.post(
            "/api/v1/usuarios/roles/", {"name": "Reponedor"}, format="json"
        )
        role_id = created.data["id"]
        grant = client.post(
            "/api/v1/usuarios/role-permissions/",
            {"role": role_id, "permission": self.manage_users_perm.id},
            format="json",
        )
        self.assertEqual(grant.status_code, 201)

        response = client.delete(f"/api/v1/usuarios/roles/{role_id}/")
        self.assertEqual(response.status_code, 204)
        self.assertTrue(Role.all_objects.filter(id=role_id).exists())

    def test_cannot_delete_system_role(self):
        response = self._client_as(self.admin_user).delete(
            f"/api/v1/usuarios/roles/{self.seller_role.id}/"
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["error"]["code"], "CANNOT_DELETE_SYSTEM_ROLE")
        self.assertTrue(Role.objects.filter(id=self.seller_role.id).exists())

    def test_cannot_delete_role_with_active_users(self):
        client = self._client_as(self.admin_user)
        created = client.post(
            "/api/v1/usuarios/roles/", {"name": "Cajero"}, format="json"
        )
        role_id = created.data["id"]
        User.objects.create(email="cajero@negocio.com", role_id=role_id)

        response = client.delete(f"/api/v1/usuarios/roles/{role_id}/")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["error"]["code"], "ROLE_IN_USE")
        self.assertTrue(Role.objects.filter(id=role_id).exists())


class PasswordResetEndpointsTests(TenantTestCase):
    """POST /auth/password-reset/ y /auth/password-reset/confirm/ (Sprint 5)."""

    @classmethod
    def get_test_schema_name(cls):
        return "test_usuarios_password_reset_views"

    @classmethod
    def get_test_tenant_domain(cls):
        return "test-usuarios-password-reset-views.test.com"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        role = Role.objects.create(name="admin", is_system_default=True)
        cls.password = "ClaveVieja123"
        cls.user = User.objects.create(email="admin@negocio.com", role=role)
        cls.user.set_password(cls.password)
        cls.user.save()

    @classmethod
    def tearDownClass(cls):
        TenantSettings.objects.filter(tenant=cls.tenant).delete()
        super().tearDownClass()

    def test_request_reset_always_returns_200(self):
        client = APIClient(HTTP_HOST=self.domain.domain)
        response = client.post(
            "/api/v1/auth/password-reset/",
            {"email": "no-existe@negocio.com"},
            format="json",
        )
        self.assertEqual(response.status_code, 200)

    def test_full_reset_flow_allows_login_with_new_password(self):
        from django.core import mail

        client = APIClient(HTTP_HOST=self.domain.domain)
        client.post(
            "/api/v1/auth/password-reset/", {"email": self.user.email}, format="json"
        )
        self.assertEqual(len(mail.outbox), 1)

        from usuarios.models import PasswordResetToken

        token = PasswordResetToken.objects.get(user=self.user)

        confirm_response = client.post(
            "/api/v1/auth/password-reset/confirm/",
            {"token": token.token, "new_password": "ClaveNueva456"},
            format="json",
        )
        self.assertEqual(confirm_response.status_code, 200)

        login_response = client.post(
            "/api/v1/auth/login/",
            {"email": self.user.email, "password": "ClaveNueva456"},
            format="json",
        )
        self.assertEqual(login_response.status_code, 200)

    def test_confirm_with_invalid_token_fails(self):
        client = APIClient(HTTP_HOST=self.domain.domain)
        response = client.post(
            "/api/v1/auth/password-reset/confirm/",
            {"token": "invalido", "new_password": "ClaveNueva456"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)


class EmployeeEndpointsTests(TenantTestCase):
    """CRUD de empleados/horarios + clock-in/clock-out (Sprint 22): gateado
    por RequiresFeature('HAS_HR_MODULE') y HR_MANAGE, igual que compras se
    gatea por HAS_PURCHASES_MODULE + PURCHASES_MANAGE."""

    @classmethod
    def get_test_schema_name(cls):
        return "test_usuarios_employees_views"

    @classmethod
    def get_test_tenant_domain(cls):
        return "test-usuarios-employees-views.test.com"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.password = "ClaveSegura123"
        cls.admin_role = Role.objects.get(name="admin")
        cls.seller_role = Role.objects.get(name="seller")

        cls.admin_user = User.objects.create(
            email="admin@negocio.com", role=cls.admin_role
        )
        cls.admin_user.set_password(cls.password)
        cls.admin_user.save()

        cls.seller_user = User.objects.create(
            email="vendedor@negocio.com", role=cls.seller_role
        )
        cls.seller_user.set_password(cls.password)
        cls.seller_user.save()

        cls.warehouse = Warehouse.objects.create(name="Principal")

    @classmethod
    def tearDownClass(cls):
        TenantSettings.objects.filter(tenant=cls.tenant).delete()
        super().tearDownClass()

    def setUp(self):
        from django.core.cache import cache

        cache.clear()
        settings = TenantSettings.objects.get(tenant=self.tenant)
        settings.hr_module_enabled = True
        settings.save(update_fields=["hr_module_enabled"])

    def _client_as(self, user):
        client = APIClient(HTTP_HOST=self.domain.domain)
        login = client.post(
            "/api/v1/auth/login/",
            {"email": user.email, "password": self.password},
            format="json",
        )
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {login.data['access']}")
        return client

    def _create_employee(self, client, document_number="12345678"):
        response = client.post(
            "/api/v1/usuarios/employees/",
            {
                "full_name": "Maria Lopez",
                "document_number": document_number,
                "position": "Vendedora",
                "warehouse": self.warehouse.id,
                "salary_type": "MONTHLY",
                "salary_amount": "1800.00",
                "hire_date": "2026-01-15",
            },
            format="json",
        )
        return response

    def test_hr_module_blocked_by_default(self):
        # hr_module_enabled=False es el default del modelo -se apaga aqui
        # para probar el otro lado del flag (setUp lo prende para el resto
        # de los tests de esta clase).
        settings = TenantSettings.objects.get(tenant=self.tenant)
        settings.hr_module_enabled = False
        settings.save(update_fields=["hr_module_enabled"])

        response = self._client_as(self.admin_user).get("/api/v1/usuarios/employees/")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data["code"], "MODULE_DISABLED")

    def test_seller_cannot_access_employees(self):
        response = self._client_as(self.seller_user).get("/api/v1/usuarios/employees/")
        self.assertEqual(response.status_code, 403)

    def test_admin_creates_employee(self):
        response = self._create_employee(self._client_as(self.admin_user))
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["full_name"], "Maria Lopez")

    def test_admin_creates_schedule_for_employee(self):
        client = self._client_as(self.admin_user)
        employee_id = self._create_employee(client).data["id"]

        response = client.post(
            "/api/v1/usuarios/employee-schedules/",
            {
                "employee": employee_id,
                "day_of_week": "MONDAY",
                "start_time": "09:00",
                "end_time": "17:00",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201)

    def test_clock_in_and_clock_out_flow(self):
        client = self._client_as(self.admin_user)
        employee_id = self._create_employee(client).data["id"]

        clock_in = client.post(
            "/api/v1/usuarios/employee-attendance/clock-in/",
            {"employee_id": employee_id, "warehouse_id": self.warehouse.id},
            format="json",
        )
        self.assertEqual(clock_in.status_code, 201)
        self.assertIn(clock_in.data["status"], ["ON_TIME", "LATE"])

        attendance_id = clock_in.data["id"]
        clock_out = client.post(
            f"/api/v1/usuarios/employee-attendance/{attendance_id}/clock-out/"
        )
        self.assertEqual(clock_out.status_code, 200)
        self.assertIsNotNone(clock_out.data["check_out"])

    def test_clock_in_twice_without_clock_out_returns_409(self):
        client = self._client_as(self.admin_user)
        employee_id = self._create_employee(client).data["id"]
        payload = {"employee_id": employee_id, "warehouse_id": self.warehouse.id}

        client.post(
            "/api/v1/usuarios/employee-attendance/clock-in/", payload, format="json"
        )
        second = client.post(
            "/api/v1/usuarios/employee-attendance/clock-in/", payload, format="json"
        )
        self.assertEqual(second.status_code, 409)

    def test_generate_and_mark_paid_payroll_flow(self):
        client = self._client_as(self.admin_user)
        employee_id = self._create_employee(client).data["id"]

        generate = client.post(
            "/api/v1/usuarios/employee-payroll/generate/",
            {
                "employee_id": employee_id,
                "period_start": "2026-08-01",
                "period_end": "2026-08-31",
                "bonuses": "50.00",
            },
            format="json",
        )
        self.assertEqual(generate.status_code, 201)
        self.assertEqual(generate.data["net_amount"], "1850.0000")
        self.assertEqual(generate.data["status"], "PENDING")

        payroll_id = generate.data["id"]
        mark_paid = client.post(
            f"/api/v1/usuarios/employee-payroll/{payroll_id}/mark-paid/",
            {"payment_date": "2026-09-01"},
            format="json",
        )
        self.assertEqual(mark_paid.status_code, 200)
        self.assertEqual(mark_paid.data["status"], "PAID")

    def test_generate_payroll_twice_for_same_period_returns_409(self):
        client = self._client_as(self.admin_user)
        employee_id = self._create_employee(client).data["id"]
        payload = {
            "employee_id": employee_id,
            "period_start": "2026-08-01",
            "period_end": "2026-08-31",
        }

        client.post(
            "/api/v1/usuarios/employee-payroll/generate/", payload, format="json"
        )
        second = client.post(
            "/api/v1/usuarios/employee-payroll/generate/", payload, format="json"
        )
        self.assertEqual(second.status_code, 409)

    def test_attendance_report_summarizes_by_employee(self):
        client = self._client_as(self.admin_user)
        employee_id = self._create_employee(client).data["id"]
        client.post(
            "/api/v1/usuarios/employee-attendance/clock-in/",
            {"employee_id": employee_id, "warehouse_id": self.warehouse.id},
            format="json",
        )

        response = client.get(
            "/api/v1/usuarios/reports/attendance/"
            "?date_from=2020-01-01&date_to=2030-01-01"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["full_name"], "Maria Lopez")

    def test_attendance_report_csv_export(self):
        client = self._client_as(self.admin_user)
        response = client.get(
            "/api/v1/usuarios/reports/attendance/",
            {"date_from": "2020-01-01", "date_to": "2030-01-01", "export": "csv"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/csv")

    def test_payroll_cost_report_totals_net_amount(self):
        client = self._client_as(self.admin_user)
        employee_id = self._create_employee(client).data["id"]
        client.post(
            "/api/v1/usuarios/employee-payroll/generate/",
            {
                "employee_id": employee_id,
                "period_start": "2026-08-01",
                "period_end": "2026-08-31",
            },
            format="json",
        )

        response = client.get(
            "/api/v1/usuarios/reports/payroll-cost/"
            "?period_start=2026-08-01&period_end=2026-08-31"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["total_net_amount"], "1800.0000")

    def test_payroll_cost_report_xlsx_export(self):
        client = self._client_as(self.admin_user)
        response = client.get(
            "/api/v1/usuarios/reports/payroll-cost/"
            "?period_start=2026-08-01&period_end=2026-08-31&export=xlsx"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response["Content-Type"],
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )


class UsersModuleTenantSuspendedGatingTests(TenantTestCase):
    """Revision general del proyecto: RoleViewSet, PermissionViewSet,
    RolePermissionViewSet, RolePermissionsHistoryViewSet, UserViewSet,
    UserPermissionViewSet, AuditLogViewSet y UserAnonymizeView importaban
    TenantNotSuspended/TenantNotCanceled pero no los usaban -un tenant
    suspendido o cancelado (fuera del periodo de gracia) podia seguir
    gestionando usuarios/roles/permisos y ver auditoria, mientras el resto
    del sistema (ventas, inventario, RRHH, gimnasio, dashboard) ya le
    quedaba bloqueado desde hace varios sprints."""

    @classmethod
    def get_test_schema_name(cls):
        return "test_users_module_gating"

    @classmethod
    def get_test_tenant_domain(cls):
        return "test-users-module-gating.test.com"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.password = "ClaveSegura123"
        admin_role = Role.objects.get(name="admin")
        cls.admin_user = User.objects.create(email="admin@negocio.com", role=admin_role)
        cls.admin_user.set_password(cls.password)
        cls.admin_user.save()

    @classmethod
    def tearDownClass(cls):
        TenantSettings.objects.filter(tenant=cls.tenant).delete()
        super().tearDownClass()

    def setUp(self):
        cache.clear()
        # Login ANTES de suspender: TenantUserLoginView es AllowAny (un
        # usuario suspendido debe poder autenticarse para enterarse de que
        # esta suspendido), asi que el token se obtiene con el tenant
        # todavia activo y luego se reusa contra el tenant ya suspendido.
        client = APIClient(HTTP_HOST=self.domain.domain)
        login = client.post(
            "/api/v1/auth/login/",
            {"email": self.admin_user.email, "password": self.password},
            format="json",
        )
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {login.data['access']}")
        self.client_as_admin = client

        self.tenant.status = "suspended"
        self.tenant.suspended_at = timezone.now()
        self.tenant.save(update_fields=["status", "suspended_at"])

    def tearDown(self):
        self.tenant.status = "active"
        self.tenant.suspended_at = None
        self.tenant.save(update_fields=["status", "suspended_at"])

    def test_role_list_is_blocked(self):
        response = self.client_as_admin.get("/api/v1/usuarios/roles/")
        self.assertEqual(response.status_code, 402)

    def test_permission_list_is_blocked(self):
        response = self.client_as_admin.get("/api/v1/usuarios/permissions/")
        self.assertEqual(response.status_code, 402)

    def test_role_permission_list_is_blocked(self):
        response = self.client_as_admin.get("/api/v1/usuarios/role-permissions/")
        self.assertEqual(response.status_code, 402)

    def test_role_permissions_history_is_blocked(self):
        response = self.client_as_admin.get(
            "/api/v1/usuarios/role-permissions-history/"
        )
        self.assertEqual(response.status_code, 402)

    def test_user_list_is_blocked(self):
        response = self.client_as_admin.get("/api/v1/usuarios/users/")
        self.assertEqual(response.status_code, 402)

    def test_user_permission_list_is_blocked(self):
        response = self.client_as_admin.get("/api/v1/usuarios/user-permissions/")
        self.assertEqual(response.status_code, 402)

    def test_audit_log_list_is_blocked(self):
        response = self.client_as_admin.get("/api/v1/usuarios/audit-logs/")
        self.assertEqual(response.status_code, 402)

    def test_user_anonymize_is_blocked(self):
        response = self.client_as_admin.post(
            f"/api/v1/usuarios/users/{self.admin_user.id}/anonymize/", {}, format="json"
        )
        self.assertEqual(response.status_code, 402)

    def test_own_data_export_is_deliberately_not_blocked(self):
        """Derecho ARCO personal -no depende de que el negocio haya pagado."""
        response = self.client_as_admin.get("/api/v1/usuarios/me/data-export/")
        self.assertEqual(response.status_code, 200)
