# Pruebas de MembershipService: ciclo de vida completo de una membresia.
from datetime import date, timedelta

from django_tenants.test.cases import TenantTestCase

from core.models import TenantSettings
from gimnasio.models import Membership, MembershipPlan
from gimnasio.services import (
    MembershipAlreadyCancelledError,
    MembershipNotActiveError,
    MembershipNotFrozenError,
    MembershipNotRenewableError,
    MembershipService,
)
from usuarios.models import Role, User
from ventas.models import Customer


class MembershipServiceTests(TenantTestCase):
    """MembershipService (Sprint 29, Ficha de Producto §5.1): crear,
    renovar, congelar/descongelar y cancelar una membresia, y las tareas
    periodicas de expiracion/aviso."""

    @classmethod
    def get_test_schema_name(cls):
        return "test_gimnasio_membership_service"

    @classmethod
    def get_test_tenant_domain(cls):
        return "test-gimnasio-membership-service.test.com"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        role = Role.objects.get(name="admin")
        cls.user = User.objects.create(email="admin@negocio.com", role=role)
        cls.customer = Customer.objects.create(
            document_type="DNI", document_number="88888888", name="Socio Uno"
        )

    @classmethod
    def tearDownClass(cls):
        TenantSettings.objects.filter(tenant=cls.tenant).delete()
        super().tearDownClass()

    def _create_plan(self, *, periodicity="MONTHLY", price="100.00"):
        return MembershipPlan.objects.create(
            name="Plan mensual", price=price, periodicity=periodicity
        )

    def test_create_membership_computes_end_date_from_periodicity(self):
        plan = self._create_plan(periodicity="MONTHLY")
        membership = MembershipService.create_membership(
            customer=self.customer,
            plan=plan,
            start_date=date(2026, 1, 15),
            user=self.user,
        )
        self.assertEqual(membership.end_date, date(2026, 2, 15))
        self.assertEqual(membership.status, "ACTIVE")

    def test_create_membership_quarterly_and_yearly_periods(self):
        quarterly_plan = self._create_plan(periodicity="QUARTERLY")
        yearly_plan = self._create_plan(periodicity="YEARLY")

        quarterly = MembershipService.create_membership(
            customer=self.customer,
            plan=quarterly_plan,
            start_date=date(2026, 1, 31),
            user=self.user,
        )
        yearly = MembershipService.create_membership(
            customer=self.customer,
            plan=yearly_plan,
            start_date=date(2026, 1, 31),
            user=self.user,
        )
        # 31 de enero + 3 meses cae en abril (30 dias) -se recorta al ultimo
        # dia valido del mes destino, no se desborda a mayo.
        self.assertEqual(quarterly.end_date, date(2026, 4, 30))
        self.assertEqual(yearly.end_date, date(2027, 1, 31))

    def test_renew_membership_extends_from_current_end_date_when_not_expired(self):
        # Fecha de inicio claramente futura -garantiza que end_date siga
        # por delante de "hoy" sin importar cuando corra el test, para que
        # renew_membership() tome la rama "todavia no vencio" (extiende
        # desde end_date, no desde hoy).
        plan = self._create_plan(periodicity="MONTHLY")
        membership = MembershipService.create_membership(
            customer=self.customer,
            plan=plan,
            start_date=date(2027, 1, 1),
            user=self.user,
        )
        self.assertEqual(membership.end_date, date(2027, 2, 1))

        renewed = MembershipService.renew_membership(
            membership=membership, user=self.user
        )
        self.assertEqual(renewed.end_date, date(2027, 3, 1))
        self.assertEqual(renewed.status, "ACTIVE")

    def test_renew_membership_with_payment_records_it(self):
        plan = self._create_plan(periodicity="MONTHLY")
        membership = MembershipService.create_membership(
            customer=self.customer, plan=plan, start_date=date.today(), user=self.user
        )
        MembershipService.renew_membership(
            membership=membership,
            user=self.user,
            payment_amount="100.00",
            payment_method="CASH",
        )
        self.assertEqual(membership.payments.count(), 1)
        self.assertEqual(membership.payments.first().amount, 100)

    def test_renew_cancelled_membership_raises(self):
        plan = self._create_plan()
        membership = MembershipService.create_membership(
            customer=self.customer, plan=plan, start_date=date.today(), user=self.user
        )
        MembershipService.cancel_membership(membership=membership, user=self.user)
        with self.assertRaises(MembershipNotRenewableError):
            MembershipService.renew_membership(membership=membership, user=self.user)

    def test_freeze_then_unfreeze_extends_end_date_by_frozen_days(self):
        plan = self._create_plan(periodicity="MONTHLY")
        membership = MembershipService.create_membership(
            customer=self.customer, plan=plan, start_date=date.today(), user=self.user
        )
        original_end = membership.end_date

        membership = MembershipService.freeze_membership(
            membership=membership, user=self.user
        )
        self.assertEqual(membership.status, "FROZEN")

        # Simula que la pausa duro 10 dias, retrocediendo frozen_since a mano
        # (en un test real no se puede avanzar el reloj del sistema).
        membership.frozen_since = date.today() - timedelta(days=10)
        membership.save(update_fields=["frozen_since"])

        membership = MembershipService.unfreeze_membership(
            membership=membership, user=self.user
        )
        self.assertEqual(membership.status, "ACTIVE")
        self.assertIsNone(membership.frozen_since)
        self.assertEqual(membership.end_date, original_end + timedelta(days=10))

    def test_freeze_non_active_membership_raises(self):
        plan = self._create_plan()
        membership = MembershipService.create_membership(
            customer=self.customer, plan=plan, start_date=date.today(), user=self.user
        )
        MembershipService.cancel_membership(membership=membership, user=self.user)
        with self.assertRaises(MembershipNotActiveError):
            MembershipService.freeze_membership(membership=membership, user=self.user)

    def test_unfreeze_non_frozen_membership_raises(self):
        plan = self._create_plan()
        membership = MembershipService.create_membership(
            customer=self.customer, plan=plan, start_date=date.today(), user=self.user
        )
        with self.assertRaises(MembershipNotFrozenError):
            MembershipService.unfreeze_membership(membership=membership, user=self.user)

    def test_cancel_already_cancelled_membership_raises(self):
        plan = self._create_plan()
        membership = MembershipService.create_membership(
            customer=self.customer, plan=plan, start_date=date.today(), user=self.user
        )
        MembershipService.cancel_membership(membership=membership, user=self.user)
        with self.assertRaises(MembershipAlreadyCancelledError):
            MembershipService.cancel_membership(membership=membership, user=self.user)

    def test_expire_overdue_memberships_marks_active_past_end_date(self):
        plan = self._create_plan()
        membership = MembershipService.create_membership(
            customer=self.customer,
            plan=plan,
            start_date=date.today() - timedelta(days=40),
            user=self.user,
        )
        expired_count = MembershipService.expire_overdue_memberships()
        self.assertEqual(expired_count, 1)
        membership.refresh_from_db()
        self.assertEqual(membership.status, "EXPIRED")

    def test_expire_overdue_memberships_never_expires_a_frozen_membership(self):
        plan = self._create_plan()
        membership = MembershipService.create_membership(
            customer=self.customer,
            plan=plan,
            start_date=date.today() - timedelta(days=40),
            user=self.user,
        )
        MembershipService.freeze_membership(membership=membership, user=self.user)

        expired_count = MembershipService.expire_overdue_memberships()

        self.assertEqual(expired_count, 0)
        membership.refresh_from_db()
        self.assertEqual(membership.status, "FROZEN")

    def test_get_expiring_soon_only_returns_active_within_window(self):
        plan = self._create_plan()
        soon = MembershipService.create_membership(
            customer=self.customer,
            plan=plan,
            start_date=date.today() - timedelta(days=25),
            user=self.user,
        )
        far = MembershipService.create_membership(
            customer=self.customer, plan=plan, start_date=date.today(), user=self.user
        )
        self.assertEqual(Membership.objects.count(), 2)

        expiring = list(MembershipService.get_expiring_soon(days=7))
        expiring_ids = [m.id for m in expiring]
        self.assertIn(soon.id, expiring_ids)
        self.assertNotIn(far.id, expiring_ids)
