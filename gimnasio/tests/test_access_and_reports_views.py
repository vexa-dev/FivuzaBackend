# Pruebas de endpoints: verificacion de acceso, QR, check-in y reportes
# de gimnasio (Sprint 31, Ficha de Producto §5.1).
from datetime import date, timedelta

from django.core.cache import cache
from django_tenants.test.cases import TenantTestCase
from rest_framework.test import APIClient

from core.models import TenantSettings
from gimnasio.models import GymClass, MembershipPlan
from gimnasio.services import AccessCheckService, ClassBookingService, MembershipService
from inventario.models import Warehouse
from usuarios.models import Employee, Role, User
from ventas.models import Customer


class AccessAndReportsEndpointsTests(TenantTestCase):
    @classmethod
    def get_test_schema_name(cls):
        return "test_gimnasio_access_reports_views"

    @classmethod
    def get_test_tenant_domain(cls):
        return "test-gimnasio-access-reports-views.test.com"

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
            document_type="DNI", document_number="32111111", name="Socio Reportes"
        )
        cls.plan = MembershipPlan.objects.create(
            name="Plan Full", price="150.00", periodicity="MONTHLY"
        )
        cls.warehouse = Warehouse.objects.create(name="Principal")
        cls.instructor = Employee.objects.create(
            full_name="Luis Vera",
            document_number="99988877",
            position="Instructor",
            warehouse=cls.warehouse,
            salary_type="MONTHLY",
            salary_amount="1800.00",
            hire_date=date(2026, 1, 1),
        )
        cls.gym_class = GymClass.objects.create(
            name="Crossfit",
            instructor=cls.instructor,
            max_capacity=1,
            duration_minutes=50,
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

    def test_access_check_allowed_for_active_membership(self):
        client = self._client()
        membership = MembershipService.create_membership(
            customer=self.customer,
            plan=self.plan,
            start_date=date.today(),
            user=self.admin_user,
        )
        response = client.get(
            f"/api/v1/gimnasio/memberships/{membership.id}/access-check/"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, {"allowed": True, "reason": None})

    def test_access_check_denied_for_frozen_membership(self):
        client = self._client()
        membership = MembershipService.create_membership(
            customer=self.customer,
            plan=self.plan,
            start_date=date.today(),
            user=self.admin_user,
        )
        MembershipService.freeze_membership(membership=membership, user=self.admin_user)
        response = client.get(
            f"/api/v1/gimnasio/memberships/{membership.id}/access-check/"
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data["allowed"])
        self.assertEqual(response.data["reason"], "MEMBERSHIP_FROZEN")

    def test_qr_endpoint_returns_png(self):
        client = self._client()
        membership = MembershipService.create_membership(
            customer=self.customer,
            plan=self.plan,
            start_date=date.today(),
            user=self.admin_user,
        )
        response = client.get(f"/api/v1/gimnasio/memberships/{membership.id}/qr/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "image/png")

    def test_check_in_by_qr_token(self):
        client = self._client()
        membership = MembershipService.create_membership(
            customer=self.customer,
            plan=self.plan,
            start_date=date.today(),
            user=self.admin_user,
        )
        token = AccessCheckService.qr_token(membership)
        response = client.post(
            "/api/v1/gimnasio/check-in/", {"token": token}, format="json"
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["allowed"])
        self.assertEqual(response.data["customer_name"], self.customer.name)

    def test_check_in_by_membership_id_manual_search(self):
        client = self._client()
        membership = MembershipService.create_membership(
            customer=self.customer,
            plan=self.plan,
            start_date=date.today(),
            user=self.admin_user,
        )
        response = client.post(
            "/api/v1/gimnasio/check-in/",
            {"membership_id": membership.id},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["allowed"])

    def test_check_in_with_invalid_token_returns_400(self):
        client = self._client()
        response = client.post(
            "/api/v1/gimnasio/check-in/", {"token": "GARBAGE"}, format="json"
        )
        self.assertEqual(response.status_code, 400)

    def test_check_in_denied_for_expired_membership(self):
        client = self._client()
        membership = MembershipService.create_membership(
            customer=self.customer,
            plan=self.plan,
            start_date=date.today(),
            user=self.admin_user,
        )
        membership.end_date = date.today() - timedelta(days=1)
        membership.save(update_fields=["end_date"])
        response = client.post(
            "/api/v1/gimnasio/check-in/",
            {"membership_id": membership.id},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data["allowed"])
        self.assertEqual(response.data["reason"], "MEMBERSHIP_EXPIRED")

    def test_class_attendance_report_computes_occupancy(self):
        client = self._client()
        booking = ClassBookingService.book_class(
            customer=self.customer,
            gym_class=self.gym_class,
            class_date=date(2026, 9, 20),
        )
        ClassBookingService.mark_attendance(booking=booking, attended=True)

        response = client.get(
            "/api/v1/gimnasio/reports/class-attendance/"
            "?date_from=2026-09-20&date_to=2026-09-20"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data), 1)
        row = response.data[0]
        self.assertEqual(row["attended_count"], 1)
        self.assertEqual(row["occupancy_pct"], 100.0)

    def test_class_attendance_report_export_csv(self):
        client = self._client()
        ClassBookingService.book_class(
            customer=self.customer,
            gym_class=self.gym_class,
            class_date=date(2026, 9, 21),
        )
        response = client.get(
            "/api/v1/gimnasio/reports/class-attendance/"
            "?date_from=2026-09-21&date_to=2026-09-21&export=csv"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/csv")

    def test_memberships_expiring_report(self):
        client = self._client()
        MembershipService.create_membership(
            customer=self.customer,
            plan=self.plan,
            start_date=date.today() - timedelta(days=25),
            user=self.admin_user,
        )
        response = client.get("/api/v1/gimnasio/reports/memberships-expiring/?days=7")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["customer_name"], self.customer.name)

    def test_revenue_by_plan_report(self):
        client = self._client()
        membership = MembershipService.create_membership(
            customer=self.customer,
            plan=self.plan,
            start_date=date.today(),
            user=self.admin_user,
        )
        MembershipService.renew_membership(
            membership=membership,
            user=self.admin_user,
            payment_amount="150.00",
            payment_method="CASH",
        )
        response = client.get(
            f"/api/v1/gimnasio/reports/revenue-by-plan/"
            f"?date_from={date.today().isoformat()}&date_to={date.today().isoformat()}"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["plan_name"], self.plan.name)
        self.assertEqual(response.data[0]["total_amount"], "150.00")
