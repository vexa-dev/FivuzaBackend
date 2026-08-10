# Pruebas de endpoints de clases y reservas de cupo.
from datetime import date

from django.core.cache import cache
from django_tenants.test.cases import TenantTestCase
from rest_framework.test import APIClient

from core.models import TenantSettings
from gimnasio.models import GymClass
from inventario.models import Warehouse
from usuarios.models import Employee, Role, User
from ventas.models import Customer


class ClassBookingEndpointsTests(TenantTestCase):
    @classmethod
    def get_test_schema_name(cls):
        return "test_gimnasio_class_booking_views"

    @classmethod
    def get_test_tenant_domain(cls):
        return "test-gimnasio-class-booking-views.test.com"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.password = "ClaveSegura123"
        cls.admin_user = User.objects.create(
            email="admin@negocio.com", role=Role.objects.get(name="admin")
        )
        cls.admin_user.set_password(cls.password)
        cls.admin_user.save()
        cls.customer = Customer.objects.create(
            document_type="DNI", document_number="88811111", name="Socio Endpoint"
        )
        cls.warehouse = Warehouse.objects.create(name="Principal")
        cls.instructor = Employee.objects.create(
            full_name="Ana Torres",
            document_number="66677788",
            position="Instructora",
            warehouse=cls.warehouse,
            salary_type="MONTHLY",
            salary_amount="1800.00",
            hire_date=date(2026, 1, 1),
        )
        cls.gym_class = GymClass.objects.create(
            name="Yoga",
            instructor=cls.instructor,
            max_capacity=1,
            duration_minutes=60,
        )

    @classmethod
    def tearDownClass(cls):
        TenantSettings.objects.filter(tenant=cls.tenant).delete()
        super().tearDownClass()

    def setUp(self):
        cache.clear()
        settings = TenantSettings.objects.get(tenant=self.tenant)
        settings.gym_module_enabled = True
        settings.save(update_fields=["gym_module_enabled"])

    def _client(self):
        client = APIClient(HTTP_HOST=self.domain.domain)
        login = client.post(
            "/api/v1/auth/login/",
            {"email": self.admin_user.email, "password": self.password},
            format="json",
        )
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {login.data['access']}")
        return client

    def test_book_class_and_mark_attendance(self):
        client = self._client()
        response = client.post(
            "/api/v1/gimnasio/class-bookings/",
            {
                "customer_id": self.customer.id,
                "gym_class_id": self.gym_class.id,
                "class_date": "2026-09-15",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        booking_id = response.data["id"]

        attend_response = client.post(
            f"/api/v1/gimnasio/class-bookings/{booking_id}/attend/",
            {"attended": True},
            format="json",
        )
        self.assertEqual(attend_response.status_code, 200)
        self.assertEqual(attend_response.data["status"], "ASISTIO")

    def test_booking_over_capacity_returns_409(self):
        client = self._client()
        other_customer = Customer.objects.create(
            document_type="DNI", document_number="88822222", name="Socio Dos"
        )
        client.post(
            "/api/v1/gimnasio/class-bookings/",
            {
                "customer_id": self.customer.id,
                "gym_class_id": self.gym_class.id,
                "class_date": "2026-09-16",
            },
            format="json",
        )
        response = client.post(
            "/api/v1/gimnasio/class-bookings/",
            {
                "customer_id": other_customer.id,
                "gym_class_id": self.gym_class.id,
                "class_date": "2026-09-16",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["error"]["code"], "CLASS_FULL")

    def test_cancelling_booking_frees_capacity_for_endpoint_flow(self):
        client = self._client()
        first = client.post(
            "/api/v1/gimnasio/class-bookings/",
            {
                "customer_id": self.customer.id,
                "gym_class_id": self.gym_class.id,
                "class_date": "2026-09-17",
            },
            format="json",
        )
        client.post(f"/api/v1/gimnasio/class-bookings/{first.data['id']}/cancel/")

        other_customer = Customer.objects.create(
            document_type="DNI", document_number="88833333", name="Socio Tres"
        )
        response = client.post(
            "/api/v1/gimnasio/class-bookings/",
            {
                "customer_id": other_customer.id,
                "gym_class_id": self.gym_class.id,
                "class_date": "2026-09-17",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201)
