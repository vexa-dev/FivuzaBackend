# Pruebas de management commands.
from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from core.models import Plan, PlanFeature


class SeedPlansCommandTests(TestCase):
    def test_creates_the_4_commercial_plans(self):
        call_command("seed_plans", stdout=StringIO())

        self.assertEqual(Plan.objects.count(), 4)
        plan_1 = Plan.objects.get(code="PLAN_1")
        self.assertEqual(plan_1.max_users, 1)
        self.assertEqual(str(plan_1.price_monthly), "29.00")

    def test_seeds_expected_features_per_plan(self):
        call_command("seed_plans", stdout=StringIO())

        plan_2 = Plan.objects.get(code="PLAN_2")
        feature_codes = set(
            plan_2.features.filter(is_enabled=True).values_list(
                "feature_code", flat=True
            )
        )
        self.assertIn("HAS_SALES_MODULE", feature_codes)

        plan_1 = Plan.objects.get(code="PLAN_1")
        self.assertNotIn(
            "HAS_SALES_MODULE",
            plan_1.features.filter(is_enabled=True).values_list(
                "feature_code", flat=True
            ),
        )

    def test_is_idempotent(self):
        call_command("seed_plans", stdout=StringIO())
        call_command("seed_plans", stdout=StringIO())

        self.assertEqual(Plan.objects.count(), 4)
        self.assertEqual(PlanFeature.objects.filter(plan__code="PLAN_1").count(), 2)
