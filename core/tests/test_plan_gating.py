# Sprint 35 (PRD S6.2): confirma que los planes comerciales reales -no solo
# TenantSettings- bloquean/habilitan el modulo que les corresponde. Los
# tests existentes de MODULE_DISABLED (uno por app) solo ejercitan la capa
# TenantSettings; ninguno crea un Plan+Subscription real, asi que la capa de
# PlanFeature -la que de verdad diferencia un plan comercial de otro- no
# tenia cobertura. Encontro un bug real: PLAN_1/PLAN_3 no bloqueaban Ventas
# porque la fila PlanFeature simplemente no existia (ver seed_plans.py).
from datetime import timedelta
from io import StringIO

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from core.models import Domain, Plan, Subscription, Tenant


class PlanBasedModuleGatingTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_plans", stdout=StringIO())
        cls.plan_1 = Plan.objects.get(code="PLAN_1")
        cls.plan_2 = Plan.objects.get(code="PLAN_2")
        cls.password = "ClaveSegura123"

    def tearDown(self):
        # El login via APIClient contra el dominio del tenant deja la
        # conexion apuntando a su schema (TenantMainMiddleware la fija por
        # request pero no la restaura) -sin este reset, el siguiente test
        # de esta clase no puede crear su propio Tenant (exige estar en
        # public). Mismo fix que Sprint 33 (core/tests/test_data_retention.py).
        from django.db import connection

        connection.set_schema_to_public()

    def _tenant_with_plan(self, schema_name: str, domain: str, plan: Plan):
        tenant = Tenant.objects.create(
            schema_name=schema_name, company_name=schema_name
        )
        Domain.objects.create(domain=domain, tenant=tenant, is_primary=True)
        Subscription.objects.create(
            tenant=tenant,
            plan=plan,
            billing_cycle="MONTHLY",
            price_paid=plan.price_monthly,
            status="active",
            starts_at=timezone.now(),
            expires_at=timezone.now() + timedelta(days=30),
        )
        return tenant

    def _login(self, domain: str):
        from django_tenants.utils import schema_context

        client = APIClient(HTTP_HOST=domain)
        with schema_context(Domain.objects.get(domain=domain).tenant.schema_name):
            from usuarios.models import Role, User

            role = Role.objects.get(name="admin")
            user = User.objects.create(email="admin@negocio.com", role=role)
            user.set_password(self.password)
            user.save()

        login = client.post(
            "/api/v1/auth/login/",
            {"email": "admin@negocio.com", "password": self.password},
            format="json",
        )
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {login.data['access']}")
        return client

    def test_plan_1_blocks_sales_module(self):
        self._tenant_with_plan(
            "test_plan1_gate", "test-plan1-gate.localhost", self.plan_1
        )
        client = self._login("test-plan1-gate.localhost")

        response = client.get("/api/v1/ventas/sales/")

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data["code"], "MODULE_DISABLED")

    def test_plan_2_allows_sales_module(self):
        self._tenant_with_plan(
            "test_plan2_gate", "test-plan2-gate.localhost", self.plan_2
        )
        client = self._login("test-plan2-gate.localhost")

        response = client.get("/api/v1/ventas/sales/")

        self.assertEqual(response.status_code, 200)
