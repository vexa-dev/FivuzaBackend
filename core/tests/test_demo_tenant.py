# Pruebas de DemoTenantService: salvaguarda is_demo, siembra y reset del
# ambiente de demostracion comercial (Sprint 32).
from django.test import TestCase

from core.models import Domain, Tenant
from core.services import DemoTenantService, TenantNotDemoError


class DemoTenantServiceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.real_tenant = Tenant.objects.create(
            schema_name="test_demo_guard_real", company_name="Negocio Real"
        )
        Domain.objects.create(
            domain="test-demo-guard-real.localhost",
            tenant=cls.real_tenant,
            is_primary=True,
        )
        cls.demo_tenant = Tenant.objects.create(
            schema_name="test_demo_guard_demo", company_name="Demo Fivuza", is_demo=True
        )
        Domain.objects.create(
            domain="test-demo-guard-demo.localhost",
            tenant=cls.demo_tenant,
            is_primary=True,
        )

        # DemoTenantService atribuye las sesiones de caja y las ventas
        # historicas a un usuario admin ya existente en el esquema -sin
        # esto, seed_demo_tenant() no generaria ni sesiones ni ventas.
        from django_tenants.utils import schema_context

        with schema_context(cls.demo_tenant.schema_name):
            from usuarios.models import Role, User

            role = Role.objects.get(name="admin")
            cls.demo_admin_user = User.objects.create(
                email="admin@demo.fivuza.com", role=role, is_active=True
            )
            cls.demo_admin_user.set_password("ClaveSegura123")
            cls.demo_admin_user.save()

    def _seed_kwargs(self):
        return {
            "product_count": 6,
            "customer_count": 3,
            "employee_count": 2,
            "sale_count": 5,
            "months_of_history": 2,
        }

    def test_seed_demo_tenant_refuses_non_demo_tenant(self):
        with self.assertRaises(TenantNotDemoError):
            DemoTenantService.seed_demo_tenant(self.real_tenant, **self._seed_kwargs())

    def test_reset_demo_tenant_refuses_non_demo_tenant(self):
        with self.assertRaises(TenantNotDemoError):
            DemoTenantService.reset_demo_tenant(self.real_tenant)

    def test_seed_demo_tenant_creates_expected_counts(self):
        counts = DemoTenantService.seed_demo_tenant(
            self.demo_tenant, **self._seed_kwargs()
        )

        self.assertEqual(counts["products"], 6)
        self.assertEqual(counts["variants"], 6)
        self.assertEqual(counts["customers"], 3)
        self.assertEqual(counts["employees"], 2)
        self.assertEqual(counts["sales"], 5)

        from django_tenants.utils import schema_context

        with schema_context(self.demo_tenant.schema_name):
            from inventario.models import Product, ProductVariant, Stock
            from usuarios.models import Employee
            from ventas.models import Customer, Sale, SaleDetail, SalePayment

            self.assertEqual(Product.objects.count(), 6)
            self.assertEqual(ProductVariant.objects.count(), 6)
            self.assertEqual(Stock.objects.count(), 6)
            self.assertEqual(Customer.objects.count(), 3)
            self.assertEqual(Employee.objects.count(), 2)
            self.assertEqual(Sale.objects.count(), 5)
            self.assertEqual(SaleDetail.objects.count(), 5)
            self.assertEqual(SalePayment.objects.count(), 5)

    def test_reset_demo_tenant_clears_and_reseeds(self):
        DemoTenantService.seed_demo_tenant(self.demo_tenant, **self._seed_kwargs())
        counts = DemoTenantService.reset_demo_tenant(
            self.demo_tenant, **self._seed_kwargs()
        )

        self.assertEqual(counts["products"], 6)

        from django_tenants.utils import schema_context

        with schema_context(self.demo_tenant.schema_name):
            from inventario.models import Product
            from ventas.models import Sale

            # Sigue habiendo exactamente 6 -no 12- porque el reset borro el
            # catalogo anterior antes de volver a sembrar.
            self.assertEqual(Product.objects.count(), 6)
            self.assertEqual(Sale.objects.count(), 5)
