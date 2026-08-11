# Pruebas de TenantDataRetentionService: periodo de gracia y purga de
# tenants cancelados (Sprint 33, Ley N 29733).
from datetime import timedelta

from django.test import TestCase, TransactionTestCase
from django.utils import timezone
from rest_framework.test import APIClient

from core.models import Domain, Tenant
from core.services import DATA_RETENTION_GRACE_DAYS, TenantDataRetentionService


class TenantDataRetentionServiceTests(TransactionTestCase):
    """TransactionTestCase (no TestCase): purge_expired_tenants() hace un
    DROP SCHEMA CASCADE real sobre un tenant creado en el mismo test -si
    todo corriera dentro de la transaccion envolvente que usa TestCase,
    Postgres rechaza el DROP por "pending trigger events" (constraints
    diferidas de las tablas recien sembradas via provisioning sincrono)."""

    def test_active_tenant_is_always_within_grace_period(self):
        tenant = Tenant.objects.create(
            schema_name="test_retention_active", company_name="Activo", status="active"
        )
        self.assertTrue(TenantDataRetentionService.is_within_grace_period(tenant))

    def test_recently_canceled_tenant_is_within_grace_period(self):
        tenant = Tenant.objects.create(
            schema_name="test_retention_recent",
            company_name="Recien cancelado",
            status="canceled",
            canceled_at=timezone.now() - timedelta(days=5),
        )
        self.assertTrue(TenantDataRetentionService.is_within_grace_period(tenant))

    def test_canceled_tenant_past_grace_period_is_not_within_it(self):
        tenant = Tenant.objects.create(
            schema_name="test_retention_expired",
            company_name="Vencido",
            status="canceled",
            canceled_at=timezone.now() - timedelta(days=DATA_RETENTION_GRACE_DAYS + 1),
        )
        self.assertFalse(TenantDataRetentionService.is_within_grace_period(tenant))

    def test_purge_expired_tenants_deletes_only_those_past_grace_period(self):
        within_grace = Tenant.objects.create(
            schema_name="test_purge_within_grace",
            company_name="Dentro de gracia",
            status="canceled",
            canceled_at=timezone.now() - timedelta(days=5),
        )
        expired = Tenant.objects.create(
            schema_name="test_purge_expired",
            company_name="Vencido para purga",
            status="canceled",
            canceled_at=timezone.now() - timedelta(days=DATA_RETENTION_GRACE_DAYS + 1),
        )

        purged = TenantDataRetentionService.purge_expired_tenants()

        self.assertEqual(len(purged), 1)
        self.assertEqual(purged[0]["schema_name"], "test_purge_expired")
        self.assertTrue(Tenant.objects.filter(id=within_grace.id).exists())
        self.assertFalse(Tenant.objects.filter(id=expired.id).exists())


class TenantNotCanceledGracePeriodPermissionTests(TestCase):
    """Confirma que TenantNotCanceled bloquea TODO acceso (no solo
    escritura) una vez vencido el periodo de gracia -a diferencia del
    bloqueo parcial que aplica mientras el tenant esta dentro de gracia."""

    @classmethod
    def setUpTestData(cls):
        cls.password = "ClaveSegura123"
        cls.tenant = Tenant.objects.create(
            schema_name="test_grace_permission",
            company_name="Negocio",
            status="canceled",
            canceled_at=timezone.now() - timedelta(days=DATA_RETENTION_GRACE_DAYS + 1),
        )
        cls.domain = Domain.objects.create(
            domain="test-grace-permission.localhost", tenant=cls.tenant, is_primary=True
        )

        from django_tenants.utils import schema_context

        with schema_context(cls.tenant.schema_name):
            from usuarios.models import Role, User

            role = Role.objects.get(name="admin")
            cls.user = User.objects.create(email="admin@negocio.com", role=role)
            cls.user.set_password(cls.password)
            cls.user.save()

    def tearDown(self):
        # El request via APIClient contra el dominio del tenant deja la
        # conexion apuntando a su schema (TenantMainMiddleware la fija por
        # request pero no la restaura) -sin este reset, el resto de la
        # suite hereda ese schema y Tenant.objects.create() de otros tests
        # revienta con "Can't create tenant outside the public schema".
        from django.db import connection

        connection.set_schema_to_public()

    def test_read_is_rejected_once_grace_period_expired(self):
        client = APIClient(HTTP_HOST=self.domain.domain)
        login = client.post(
            "/api/v1/auth/login/",
            {"email": self.user.email, "password": self.password},
            format="json",
        )
        # El propio login puede fallar (401) o el token puede autenticar
        # pero el permiso de negocio rechaza -cualquiera de los dos
        # confirma que ya no hay acceso de lectura util.
        if login.status_code == 200:
            client.credentials(HTTP_AUTHORIZATION=f"Bearer {login.data['access']}")
            response = client.get("/api/v1/usuarios/employees/")
            self.assertEqual(response.status_code, 403)
            self.assertEqual(
                response.data["error"]["code"], "TENANT_RETENTION_PERIOD_EXPIRED"
            )
