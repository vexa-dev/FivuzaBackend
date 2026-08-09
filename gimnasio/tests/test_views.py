# Pruebas de endpoints: permisos (HAS_GYM_MODULE + GYM_MANAGE), CRUD y
# acciones de ciclo de vida de una membresia.
from datetime import date

from django.core.cache import cache
from django_tenants.test.cases import TenantTestCase
from rest_framework.test import APIClient

from core.models import TenantSettings
from gimnasio.models import MembershipPlan
from usuarios.models import Role, User
from ventas.models import Customer


class MembershipEndpointsTests(TenantTestCase):
    @classmethod
    def get_test_schema_name(cls):
        return "test_gimnasio_membership_views"

    @classmethod
    def get_test_tenant_domain(cls):
        return "test-gimnasio-membership-views.test.com"

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
            document_type="DNI", document_number="99999999", name="Socio Endpoint"
        )
        cls.plan = MembershipPlan.objects.create(
            name="Plan mensual", price="100.00", periodicity="MONTHLY"
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

    def test_gym_module_blocked_by_default_feature_flag(self):
        settings = TenantSettings.objects.get(tenant=self.tenant)
        settings.gym_module_enabled = False
        settings.save(update_fields=["gym_module_enabled"])

        response = self._client().get("/api/v1/gimnasio/memberships/")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data["code"], "MODULE_DISABLED")

    def test_create_and_list_membership_plan(self):
        client = self._client()
        response = client.post(
            "/api/v1/gimnasio/membership-plans/",
            {
                "name": "Plan anual",
                "price": "900.00",
                "periodicity": "YEARLY",
                "benefits": "Acceso full",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201)

        listing = client.get("/api/v1/gimnasio/membership-plans/")
        self.assertEqual(listing.status_code, 200)
        self.assertGreaterEqual(len(listing.data), 2)

    def test_create_membership_computes_end_date(self):
        client = self._client()
        response = client.post(
            "/api/v1/gimnasio/memberships/",
            {
                "customer_id": self.customer.id,
                "plan_id": self.plan.id,
                "start_date": "2026-01-15",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["end_date"], "2026-02-15")
        self.assertEqual(response.data["status"], "ACTIVE")

    def test_freeze_renew_unfreeze_cancel_lifecycle(self):
        client = self._client()
        create_response = client.post(
            "/api/v1/gimnasio/memberships/",
            {
                "customer_id": self.customer.id,
                "plan_id": self.plan.id,
                "start_date": date.today().isoformat(),
            },
            format="json",
        )
        membership_id = create_response.data["id"]

        freeze_response = client.post(
            f"/api/v1/gimnasio/memberships/{membership_id}/freeze/"
        )
        self.assertEqual(freeze_response.status_code, 200)
        self.assertEqual(freeze_response.data["status"], "FROZEN")

        unfreeze_response = client.post(
            f"/api/v1/gimnasio/memberships/{membership_id}/unfreeze/"
        )
        self.assertEqual(unfreeze_response.status_code, 200)
        self.assertEqual(unfreeze_response.data["status"], "ACTIVE")

        renew_response = client.post(
            f"/api/v1/gimnasio/memberships/{membership_id}/renew/",
            {"payment_amount": "100.00", "payment_method": "CASH"},
            format="json",
        )
        self.assertEqual(renew_response.status_code, 200)
        self.assertEqual(len(renew_response.data["payments"]), 1)

        cancel_response = client.post(
            f"/api/v1/gimnasio/memberships/{membership_id}/cancel/"
        )
        self.assertEqual(cancel_response.status_code, 200)
        self.assertEqual(cancel_response.data["status"], "CANCELLED")

    def test_freeze_already_frozen_membership_returns_409(self):
        client = self._client()
        create_response = client.post(
            "/api/v1/gimnasio/memberships/",
            {
                "customer_id": self.customer.id,
                "plan_id": self.plan.id,
                "start_date": date.today().isoformat(),
            },
            format="json",
        )
        membership_id = create_response.data["id"]
        client.post(f"/api/v1/gimnasio/memberships/{membership_id}/freeze/")

        response = client.post(f"/api/v1/gimnasio/memberships/{membership_id}/freeze/")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["error"]["code"], "MEMBERSHIP_NOT_ACTIVE")

    def test_filter_memberships_by_customer_and_status(self):
        client = self._client()
        client.post(
            "/api/v1/gimnasio/memberships/",
            {
                "customer_id": self.customer.id,
                "plan_id": self.plan.id,
                "start_date": date.today().isoformat(),
            },
            format="json",
        )
        response = client.get(
            f"/api/v1/gimnasio/memberships/?customer={self.customer.id}&status=ACTIVE"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data), 1)
