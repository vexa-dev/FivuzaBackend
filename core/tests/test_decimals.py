from decimal import Decimal

from django.test import SimpleTestCase

from core.decimals import round2


class Round2Tests(SimpleTestCase):
    """Regla unica de decimales del sistema (core.decimals)."""

    def test_rounds_half_up(self):
        self.assertEqual(round2(Decimal("1.575")), Decimal("1.58"))
        self.assertEqual(round2(Decimal("12.957")), Decimal("12.96"))
        self.assertEqual(round2(Decimal("12.954")), Decimal("12.95"))

    def test_negative_ties_go_away_from_zero(self):
        self.assertEqual(round2(Decimal("-1.575")), Decimal("-1.58"))

    def test_always_two_decimal_places(self):
        self.assertEqual(str(round2(Decimal("20"))), "20.00")
        self.assertEqual(str(round2(0)), "0.00")
        self.assertEqual(str(round2("3.5")), "3.50")
