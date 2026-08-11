# Pruebas de PersonalDataService: derechos ARCO de un usuario (Sprint 33,
# Ley N 29733).
from django_tenants.test.cases import TenantTestCase
from rest_framework.test import APIClient

from core.models import TenantSettings
from usuarios.models import Employee, Role, User
from usuarios.services import PersonalDataService, UserAlreadyAnonymizedError


class PersonalDataServiceTests(TenantTestCase):
    @classmethod
    def get_test_schema_name(cls):
        return "test_personal_data_service"

    @classmethod
    def get_test_tenant_domain(cls):
        return "test-personal-data-service.test.com"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.role = Role.objects.get(name="admin")
        cls.user = User.objects.create(email="socio@negocio.com", role=cls.role)

    @classmethod
    def tearDownClass(cls):
        TenantSettings.objects.filter(tenant=cls.tenant).delete()
        super().tearDownClass()

    def test_export_own_data_includes_profile_and_permissions(self):
        data = PersonalDataService.export_own_data(self.user)
        self.assertEqual(data["email"], "socio@negocio.com")
        self.assertEqual(data["role"], "admin")
        self.assertEqual(data["permission_overrides"], [])
        self.assertNotIn("password", data)

    def test_export_own_data_includes_linked_employee(self):
        from datetime import date

        from inventario.models import Warehouse

        warehouse = Warehouse.objects.create(name="Principal")
        user = User.objects.create(email="empleado@negocio.com", role=self.role)
        Employee.objects.create(
            user=user,
            full_name="Juan Perez",
            document_number="12345678",
            position="Cajero",
            warehouse=warehouse,
            salary_type="MONTHLY",
            salary_amount="1500.00",
            hire_date=date(2026, 1, 1),
        )

        data = PersonalDataService.export_own_data(user)
        self.assertEqual(data["employee"]["full_name"], "Juan Perez")
        self.assertEqual(data["employee"]["document_number"], "12345678")

    def test_anonymize_user_scrubs_email_and_deactivates(self):
        user = User.objects.create(email="borrar@negocio.com", role=self.role)
        user.set_password("ClaveSegura123")
        user.save()

        anonymized = PersonalDataService.anonymize_user(user)

        self.assertTrue(anonymized.email.startswith("usuario-eliminado-"))
        self.assertFalse(anonymized.is_active)
        self.assertFalse(anonymized.check_password("ClaveSegura123"))

    def test_anonymize_user_scrubs_linked_employee(self):
        from datetime import date

        from inventario.models import Warehouse

        warehouse = Warehouse.objects.create(name="Principal 2")
        user = User.objects.create(email="empleado2@negocio.com", role=self.role)
        employee = Employee.objects.create(
            user=user,
            full_name="Maria Lopez",
            document_number="87654321",
            position="Vendedor",
            warehouse=warehouse,
            salary_type="MONTHLY",
            salary_amount="1500.00",
            hire_date=date(2026, 1, 1),
        )

        PersonalDataService.anonymize_user(user)
        employee.refresh_from_db()
        self.assertNotEqual(employee.full_name, "Maria Lopez")
        self.assertEqual(employee.document_number, f"ANON-{employee.id}")

    def test_anonymize_already_anonymized_user_raises(self):
        user = User.objects.create(email="doble@negocio.com", role=self.role)
        PersonalDataService.anonymize_user(user)
        with self.assertRaises(UserAlreadyAnonymizedError):
            PersonalDataService.anonymize_user(user)


class PersonalDataEndpointsTests(TenantTestCase):
    @classmethod
    def get_test_schema_name(cls):
        return "test_personal_data_views"

    @classmethod
    def get_test_tenant_domain(cls):
        return "test-personal-data-views.test.com"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.password = "ClaveSegura123"
        cls.admin_user = User.objects.create(
            email="admin@negocio.com", role=Role.objects.get(name="admin")
        )
        cls.admin_user.set_password(cls.password)
        cls.admin_user.save()

    @classmethod
    def tearDownClass(cls):
        TenantSettings.objects.filter(tenant=cls.tenant).delete()
        super().tearDownClass()

    def _client(self):
        client = APIClient(HTTP_HOST=self.domain.domain)
        login = client.post(
            "/api/v1/auth/login/",
            {"email": self.admin_user.email, "password": self.password},
            format="json",
        )
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {login.data['access']}")
        return client

    def test_own_data_export_endpoint_returns_caller_profile(self):
        response = self._client().get("/api/v1/usuarios/me/data-export/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["email"], self.admin_user.email)

    def test_anonymize_endpoint_requires_users_manage_permission(self):
        target = User.objects.create(
            email="target@negocio.com", role=Role.objects.get(name="seller")
        )
        response = self._client().post(f"/api/v1/usuarios/users/{target.id}/anonymize/")
        self.assertEqual(response.status_code, 200)
        target.refresh_from_db()
        self.assertTrue(target.email.startswith("usuario-eliminado-"))
