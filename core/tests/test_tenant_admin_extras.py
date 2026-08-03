# Notas internas, descuentos de suscripcion, onboarding, salud y consumo
# por tenant (Sprint 11, Especificacion de API §4.25-4.26).
from unittest.mock import patch

from django.utils import timezone
from django_tenants.utils import schema_context
from rest_framework.test import APIClient, APITestCase

from core.models import (
    Domain,
    Plan,
    PlatformStaff,
    Subscription,
    Tenant,
)


class TenantAdminExtrasTestBase(APITestCase):
    @classmethod
    def setUpTestData(cls):
        public_tenant = Tenant.objects.create(
            schema_name="public", company_name="Servicio Publico"
        )
        Domain.objects.create(
            domain="public.localhost", tenant=public_tenant, is_primary=True
        )
        cls.password = "ClaveSegura123"
        cls.super_admin = PlatformStaff.objects.create(
            email="admin@fivuza.com", full_name="Super Admin", role="SUPER_ADMIN"
        )
        cls.super_admin.set_password(cls.password)
        cls.super_admin.save()
        cls.support = PlatformStaff.objects.create(
            email="soporte@fivuza.com", full_name="Soporte", role="SUPPORT"
        )
        cls.support.set_password(cls.password)
        cls.support.save()
        cls.billing = PlatformStaff.objects.create(
            email="facturacion@fivuza.com", full_name="Billing", role="BILLING"
        )
        cls.billing.set_password(cls.password)
        cls.billing.save()

        cls.tenant = Tenant.objects.create(
            schema_name="test_admin_extras", company_name="Negocio Extras"
        )
        cls.plan = Plan.objects.create(
            code="PLAN_EXTRAS",
            name="Plan Extras",
            max_users=5,
            price_monthly="100.00",
            price_semiannual="500.00",
            price_annual="900.00",
        )

    def _client_as(self, staff):
        client = APIClient(HTTP_HOST="public.localhost")
        login = client.post(
            "/api/v1/platform/auth/login/",
            {"email": staff.email, "password": self.password},
            format="json",
        )
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {login.data['access']}")
        return client


class TenantNoteTests(TenantAdminExtrasTestBase):
    def test_any_staff_can_add_and_list_notes(self):
        client = self._client_as(self.support)
        response = client.post(
            f"/api/v1/core/tenants/{self.tenant.id}/notes/",
            {"text": "Cliente pidio factura electronica."},
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["platform_staff"]["full_name"], "Soporte")

        listed = client.get(f"/api/v1/core/tenants/{self.tenant.id}/notes/")
        self.assertEqual(len(listed.data), 1)
        self.assertEqual(listed.data[0]["text"], "Cliente pidio factura electronica.")

    def test_empty_note_is_rejected(self):
        response = self._client_as(self.super_admin).post(
            f"/api/v1/core/tenants/{self.tenant.id}/notes/",
            {"text": "  "},
            format="json",
        )
        self.assertEqual(response.status_code, 400)


class SubscriptionDiscountTests(TenantAdminExtrasTestBase):
    def setUp(self):
        self.subscription = Subscription.objects.create(
            tenant=self.tenant,
            plan=self.plan,
            billing_cycle="MONTHLY",
            price_paid="100.00",
            status="active",
            starts_at=timezone.now(),
            expires_at=timezone.now() + timezone.timedelta(days=30),
        )

    def test_support_cannot_create_discount(self):
        response = self._client_as(self.support).post(
            "/api/v1/core/subscription-discounts/",
            {
                "subscription_id": self.subscription.id,
                "discount_percent": 20,
                "reason": "x",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 403)

    def test_billing_can_create_percent_discount(self):
        response = self._client_as(self.billing).post(
            "/api/v1/core/subscription-discounts/",
            {
                "subscription_id": self.subscription.id,
                "discount_percent": 20,
                "reason": "Negociacion por ser tenant piloto",
                "expires_at": "2027-06-01T00:00:00Z",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["subscription_id"], self.subscription.id)
        self.assertEqual(str(response.data["discount_percent"]), "20.00")

    def test_both_discount_types_at_once_is_rejected(self):
        response = self._client_as(self.super_admin).post(
            "/api/v1/core/subscription-discounts/",
            {
                "subscription_id": self.subscription.id,
                "discount_percent": 20,
                "override_price": "50.00",
                "reason": "x",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["error"]["code"], "INVALID_DISCOUNT")

    def test_neither_discount_type_is_rejected(self):
        response = self._client_as(self.super_admin).post(
            "/api/v1/core/subscription-discounts/",
            {"subscription_id": self.subscription.id, "reason": "x"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)

    def test_discount_is_listed_filtered_by_subscription_and_can_be_removed(self):
        client = self._client_as(self.super_admin)
        created = client.post(
            "/api/v1/core/subscription-discounts/",
            {
                "subscription_id": self.subscription.id,
                "override_price": "60.00",
                "reason": "x",
            },
            format="json",
        )
        listed = client.get(
            f"/api/v1/core/subscription-discounts/?subscription={self.subscription.id}"
        )
        self.assertEqual(len(listed.data), 1)

        deleted = client.delete(
            f"/api/v1/core/subscription-discounts/{created.data['id']}/"
        )
        self.assertEqual(deleted.status_code, 204)
        listed_after = client.get(
            f"/api/v1/core/subscription-discounts/?subscription={self.subscription.id}"
        )
        self.assertEqual(len(listed_after.data), 0)

    def test_confirming_payment_applies_active_percent_discount_to_price_paid(self):
        from core.models import SubscriptionPayment

        self._client_as(self.billing).post(
            "/api/v1/core/subscription-discounts/",
            {
                "subscription_id": self.subscription.id,
                "discount_percent": "25.00",
                "reason": "x",
            },
            format="json",
        )
        payment = SubscriptionPayment.objects.create(
            subscription=self.subscription,
            amount="75.00",
            payment_method="TRANSFER",
            status="PENDING",
        )

        response = self._client_as(self.billing).post(
            f"/api/v1/core/subscription-payments/{payment.id}/confirm/",
            {},
            format="json",
        )
        self.assertEqual(response.status_code, 200)

        self.subscription.refresh_from_db()
        # plan.price_monthly=100.00, 25% off -> 75.00
        self.assertEqual(str(self.subscription.price_paid), "75.00")

    def test_confirming_payment_applies_active_override_price_discount(self):
        from core.models import SubscriptionPayment

        self._client_as(self.super_admin).post(
            "/api/v1/core/subscription-discounts/",
            {
                "subscription_id": self.subscription.id,
                "override_price": "42.00",
                "reason": "x",
            },
            format="json",
        )
        payment = SubscriptionPayment.objects.create(
            subscription=self.subscription,
            amount="42.00",
            payment_method="TRANSFER",
            status="PENDING",
        )

        self._client_as(self.billing).post(
            f"/api/v1/core/subscription-payments/{payment.id}/confirm/",
            {},
            format="json",
        )

        self.subscription.refresh_from_db()
        self.assertEqual(str(self.subscription.price_paid), "42.00")

    def test_confirming_payment_without_discount_leaves_price_paid_unchanged(self):
        from core.models import SubscriptionPayment

        payment = SubscriptionPayment.objects.create(
            subscription=self.subscription,
            amount="100.00",
            payment_method="TRANSFER",
            status="PENDING",
        )
        self._client_as(self.billing).post(
            f"/api/v1/core/subscription-payments/{payment.id}/confirm/",
            {},
            format="json",
        )
        self.subscription.refresh_from_db()
        self.assertEqual(str(self.subscription.price_paid), "100.00")

    def test_expired_discount_is_not_applied(self):
        from core.models import SubscriptionPayment

        self._client_as(self.super_admin).post(
            "/api/v1/core/subscription-discounts/",
            {
                "subscription_id": self.subscription.id,
                "discount_percent": "50.00",
                "reason": "x",
                "expires_at": "2020-01-01T00:00:00Z",
            },
            format="json",
        )
        payment = SubscriptionPayment.objects.create(
            subscription=self.subscription,
            amount="100.00",
            payment_method="TRANSFER",
            status="PENDING",
        )
        self._client_as(self.billing).post(
            f"/api/v1/core/subscription-payments/{payment.id}/confirm/",
            {},
            format="json",
        )
        self.subscription.refresh_from_db()
        self.assertEqual(str(self.subscription.price_paid), "100.00")


class TenantOnboardingConsumptionTests(TenantAdminExtrasTestBase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        with schema_context(cls.tenant.schema_name):
            from inventario.models import Category, Product
            from usuarios.models import Role, User
            from ventas.models import Customer, Sale

            cls.user = User.objects.create(
                email="admin@negocio.com", role=Role.objects.get(name="admin")
            )
            category = Category.objects.create(name="Ropa")
            Product.objects.create(
                type="PRODUCT",
                name="Polo",
                category=category,
                unit_of_measure="UND",
            )
            customer = Customer.objects.create(
                document_type="DNI", document_number="12345678", name="Cliente"
            )
            from inventario.models import Warehouse

            warehouse = Warehouse.objects.create(name="Principal")
            Sale.objects.create(
                invoice_number="NV-0001",
                customer=customer,
                user=cls.user,
                warehouse=warehouse,
                subtotal="10.00",
                total="10.00",
                payment_status="PAID",
                status="COMPLETED",
                client_side_uuid="uuid-1",
                sync_status="SYNCED",
            )

    def test_onboarding_checklist_reflects_real_state(self):
        response = self._client_as(self.super_admin).get(
            f"/api/v1/core/tenants/{self.tenant.id}/onboarding/"
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["has_catalog"])
        self.assertTrue(response.data["has_first_sale"])
        self.assertTrue(response.data["has_users_created"])

    def test_onboarding_checklist_false_for_empty_tenant(self):
        empty_tenant = Tenant.objects.create(
            schema_name="test_admin_extras_empty", company_name="Negocio Vacio"
        )
        response = self._client_as(self.super_admin).get(
            f"/api/v1/core/tenants/{empty_tenant.id}/onboarding/"
        )
        self.assertFalse(response.data["has_catalog"])
        self.assertFalse(response.data["has_first_sale"])
        self.assertFalse(response.data["has_users_created"])

    def test_onboarding_consumption_health_do_not_crash_for_public_tenant(self):
        # El tenant "public" (esquema compartido, creado en setUpTestData de
        # TenantAdminExtrasTestBase) no tiene las tablas de negocio -antes
        # de este guard, estos 3 endpoints reventaban con 500
        # "relation does not exist" en vez de responder con datos vacios.
        public_tenant = Tenant.objects.get(schema_name="public")
        client = self._client_as(self.super_admin)

        onboarding = client.get(f"/api/v1/core/tenants/{public_tenant.id}/onboarding/")
        self.assertEqual(onboarding.status_code, 200)
        self.assertFalse(onboarding.data["has_catalog"])

        consumption = client.get(
            f"/api/v1/core/tenants/{public_tenant.id}/consumption/"
        )
        self.assertEqual(consumption.status_code, 200)
        self.assertEqual(consumption.data["catalog_size"], 0)

        health = client.get(f"/api/v1/core/tenants/{public_tenant.id}/health/")
        self.assertEqual(health.status_code, 200)
        self.assertIsNone(health.data["last_sale_at"])

    def test_consumption_report_counts_real_data(self):
        response = self._client_as(self.super_admin).get(
            f"/api/v1/core/tenants/{self.tenant.id}/consumption/"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["sales_count_last_30_days"], 1)
        self.assertEqual(response.data["catalog_size"], 1)
        self.assertEqual(response.data["active_users_count"], 1)

    def test_health_endpoint_reports_own_activity_without_sentry_configured(self):
        response = self._client_as(self.super_admin).get(
            f"/api/v1/core/tenants/{self.tenant.id}/health/"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["recent_errors_count"], 0)
        self.assertIsNone(response.data["last_error_at"])
        self.assertIsNotNone(response.data["last_sale_at"])

    def test_health_endpoint_uses_sentry_api_when_configured(self):
        with (
            self.settings(
                SENTRY_API_TOKEN="tok",
                SENTRY_ORG_SLUG="org",
                SENTRY_PROJECT_SLUG="proj",
            ),
            patch("core.services.TenantHealthService._fetch_sentry_errors") as mocked,
        ):
            mocked.return_value = {
                "recent_errors_count": 3,
                "last_error_at": "2027-01-04T22:10:00Z",
            }
            response = self._client_as(self.super_admin).get(
                f"/api/v1/core/tenants/{self.tenant.id}/health/"
            )
        self.assertEqual(response.data["recent_errors_count"], 3)
        self.assertEqual(response.data["last_error_at"], "2027-01-04T22:10:00Z")
        mocked.assert_called_once_with(self.tenant.schema_name)
