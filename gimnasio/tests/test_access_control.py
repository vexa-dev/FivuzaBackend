# Pruebas de AccessCheckService: acceso permitido/denegado segun estado
# de la membresia, y la credencial QR (Sprint 31, Ficha de Producto §5.1).
from datetime import date, timedelta

from django_tenants.test.cases import TenantTestCase

from core.models import TenantSettings
from gimnasio.models import MembershipPlan
from gimnasio.services import (
    AccessCheckService,
    InvalidMembershipQrTokenError,
    MembershipService,
)
from usuarios.models import Role, User
from ventas.models import Customer


class AccessCheckServiceTests(TenantTestCase):
    @classmethod
    def get_test_schema_name(cls):
        return "test_gimnasio_access_check_service"

    @classmethod
    def get_test_tenant_domain(cls):
        return "test-gimnasio-access-check-service.test.com"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        role = Role.objects.get(name="admin")
        cls.user = User.objects.create(email="admin@negocio.com", role=role)
        cls.customer = Customer.objects.create(
            document_type="DNI", document_number="31111111", name="Socio Acceso"
        )
        cls.plan = MembershipPlan.objects.create(
            name="Plan Full", price="100.00", periodicity="MONTHLY"
        )

    @classmethod
    def tearDownClass(cls):
        TenantSettings.objects.filter(tenant=cls.tenant).delete()
        super().tearDownClass()

    def _membership(self, **overrides):
        membership = MembershipService.create_membership(
            customer=self.customer,
            plan=self.plan,
            start_date=date.today(),
            user=self.user,
        )
        for field, value in overrides.items():
            setattr(membership, field, value)
        if overrides:
            membership.save()
        return membership

    def test_active_membership_within_end_date_is_allowed(self):
        membership = self._membership()
        result = AccessCheckService.check_access(membership)
        self.assertEqual(result, {"allowed": True, "reason": None})

    def test_active_membership_past_end_date_is_denied_as_expired(self):
        membership = self._membership(end_date=date.today() - timedelta(days=1))
        result = AccessCheckService.check_access(membership)
        self.assertEqual(result, {"allowed": False, "reason": "MEMBERSHIP_EXPIRED"})

    def test_frozen_membership_is_denied(self):
        membership = self._membership(status="FROZEN")
        result = AccessCheckService.check_access(membership)
        self.assertEqual(result, {"allowed": False, "reason": "MEMBERSHIP_FROZEN"})

    def test_cancelled_membership_is_denied(self):
        membership = self._membership(status="CANCELLED")
        result = AccessCheckService.check_access(membership)
        self.assertEqual(result, {"allowed": False, "reason": "MEMBERSHIP_CANCELLED"})

    def test_expired_status_membership_is_denied(self):
        membership = self._membership(status="EXPIRED")
        result = AccessCheckService.check_access(membership)
        self.assertEqual(result, {"allowed": False, "reason": "MEMBERSHIP_EXPIRED"})

    def test_qr_token_roundtrip(self):
        membership = self._membership()
        token = AccessCheckService.qr_token(membership)
        self.assertEqual(AccessCheckService.parse_qr_token(token), membership.id)

    def test_parse_invalid_qr_token_raises(self):
        with self.assertRaises(InvalidMembershipQrTokenError):
            AccessCheckService.parse_qr_token("NOT-A-VALID-TOKEN")

    def test_generate_qr_png_returns_valid_png_bytes(self):
        membership = self._membership()
        content = AccessCheckService.generate_qr_png(membership)
        self.assertTrue(content.startswith(b"\x89PNG"))
