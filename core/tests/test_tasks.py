# Pruebas de tareas de Celery propias de core.
from datetime import timedelta

from django.core import mail
from django.test import TestCase
from django.utils import timezone

from core.models import Plan, Subscription, Tenant
from core.tasks import check_subscription_expirations


class CheckSubscriptionExpirationsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.plan = Plan.objects.create(
            code="PLAN_EXPIRING",
            name="Plan Expiring",
            max_users=1,
            price_monthly=39,
            price_semiannual=195,
            price_annual=390,
        )

    def _create_subscription(self, *, schema_name, status, expires_at):
        tenant = Tenant.objects.create(
            schema_name=schema_name, company_name=f"Negocio {schema_name}"
        )
        return Subscription.objects.create(
            tenant=tenant,
            plan=self.plan,
            billing_cycle="MONTHLY",
            price_paid=39,
            status=status,
            starts_at=timezone.now() - timedelta(days=23),
            expires_at=expires_at,
        )

    def test_warns_subscriptions_expiring_in_7_days(self):
        subscription = self._create_subscription(
            schema_name="test_expiring_soon",
            status="active",
            expires_at=timezone.now() + timedelta(days=7),
        )

        check_subscription_expirations()

        self.assertEqual(len(mail.outbox), 1)
        self.assertIn(subscription.tenant.company_name, mail.outbox[0].body)

    def test_does_not_warn_subscriptions_far_from_expiring(self):
        self._create_subscription(
            schema_name="test_expiring_far",
            status="active",
            expires_at=timezone.now() + timedelta(days=20),
        )

        check_subscription_expirations()

        self.assertEqual(len(mail.outbox), 0)

    def test_marks_expired_active_subscriptions_as_past_due(self):
        subscription = self._create_subscription(
            schema_name="test_expired",
            status="active",
            expires_at=timezone.now() - timedelta(days=1),
        )

        check_subscription_expirations()

        subscription.refresh_from_db()
        self.assertEqual(subscription.status, "past_due")

    def test_does_not_touch_already_past_due_subscriptions(self):
        subscription = self._create_subscription(
            schema_name="test_already_past_due",
            status="past_due",
            expires_at=timezone.now() - timedelta(days=40),
        )

        check_subscription_expirations()

        subscription.refresh_from_db()
        self.assertEqual(subscription.status, "past_due")
