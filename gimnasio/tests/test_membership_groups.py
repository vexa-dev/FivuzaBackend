# Pruebas de MembershipGroupService: membresias familiares/grupales.
from datetime import date

from django_tenants.test.cases import TenantTestCase

from core.models import TenantSettings
from gimnasio.models import MembershipPlan
from gimnasio.services import (
    MembershipGroupSizeError,
    MembershipGroupService,
    MembershipService,
)
from usuarios.models import Role, User
from ventas.models import Customer


class MembershipGroupServiceTests(TenantTestCase):
    """MembershipGroupService (Sprint 30, Ficha de Producto §5.1): agrupa
    2+ Membership bajo un solo titular de pago, sin alterar el ciclo de
    vida individual de cada membresia."""

    @classmethod
    def get_test_schema_name(cls):
        return "test_gimnasio_membership_group_service"

    @classmethod
    def get_test_tenant_domain(cls):
        return "test-gimnasio-membership-group-service.test.com"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        role = Role.objects.get(name="admin")
        cls.user = User.objects.create(email="admin@negocio.com", role=role)
        cls.plan = MembershipPlan.objects.create(
            name="Plan familiar", price="90.00", periodicity="MONTHLY"
        )

    @classmethod
    def tearDownClass(cls):
        TenantSettings.objects.filter(tenant=cls.tenant).delete()
        super().tearDownClass()

    def _customer(self, document_number, name):
        return Customer.objects.create(
            document_type="DNI", document_number=document_number, name=name
        )

    def _membership(self, customer):
        return MembershipService.create_membership(
            customer=customer,
            plan=self.plan,
            start_date=date(2026, 9, 1),
            user=self.user,
        )

    def test_create_group_links_all_memberships_to_it(self):
        holder = self._customer("21111111", "Titular")
        membership_a = self._membership(self._customer("21222222", "Hijo A"))
        membership_b = self._membership(self._customer("21333333", "Hijo B"))

        group = MembershipGroupService.create_group(
            holder_customer=holder,
            name="Familia Rojas",
            memberships=[membership_a, membership_b],
        )

        membership_a.refresh_from_db()
        membership_b.refresh_from_db()
        self.assertEqual(membership_a.group_id, group.id)
        self.assertEqual(membership_b.group_id, group.id)
        self.assertEqual(group.holder_customer, holder)

    def test_create_group_with_fewer_than_two_memberships_raises(self):
        holder = self._customer("22111111", "Titular")
        membership_a = self._membership(self._customer("22222222", "Hijo A"))

        with self.assertRaises(MembershipGroupSizeError):
            MembershipGroupService.create_group(
                holder_customer=holder, name="Familia", memberships=[membership_a]
            )

    def test_membership_keeps_its_own_lifecycle_after_grouping(self):
        holder = self._customer("23111111", "Titular")
        membership_a = self._membership(self._customer("23222222", "Hijo A"))
        membership_b = self._membership(self._customer("23333333", "Hijo B"))
        MembershipGroupService.create_group(
            holder_customer=holder,
            name="Familia",
            memberships=[membership_a, membership_b],
        )

        # Congelar una membresia del grupo no afecta a la otra -cada una
        # sigue su propio ciclo de vida, el grupo solo agrupa el cobro.
        MembershipService.freeze_membership(membership=membership_a, user=self.user)
        membership_b.refresh_from_db()
        self.assertEqual(membership_a.status, "FROZEN")
        self.assertEqual(membership_b.status, "ACTIVE")
