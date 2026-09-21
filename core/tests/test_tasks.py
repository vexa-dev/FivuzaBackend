# Pruebas de tareas de Celery propias de core.
from datetime import datetime, timedelta, timezone as datetime_timezone
from unittest import mock

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

    def test_warns_at_night_in_lima_when_utc_is_already_the_next_day(self):
        """La BD guarda UTC y el filtro compara la fecha ya convertida a la
        zona del proyecto (America/Lima). Entre las 19:00 y las 23:59 de
        Lima el dia UTC ya avanzo, y el aviso se perdia: la tarea buscaba
        una fecha y la columna respondia con la del dia anterior.

        Instante fijo, no la hora real de la corrida: si no, la prueba solo
        cubriria el caso segun la hora a la que se ejecute el CI.
        """
        # 2026-09-21 02:00 UTC = 2026-09-20 21:00 en Lima.
        frozen_now = datetime(2026, 9, 21, 2, 0, tzinfo=datetime_timezone.utc)
        # Vence el 2026-09-27 a las 20:00 de Lima, o sea 7 dias despues del
        # dia en curso en Lima (2026-09-20).
        expires_at = datetime(2026, 9, 28, 1, 0, tzinfo=datetime_timezone.utc)
        subscription = self._create_subscription(
            schema_name="test_expiring_night",
            status="active",
            expires_at=expires_at,
        )

        with mock.patch("django.utils.timezone.now", return_value=frozen_now):
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
