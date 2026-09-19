# Pruebas del arranque de un entorno nuevo (Railway): bootstrap_platform y
# el healthcheck independiente del Host.
import os
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import Client, TestCase

from core.models import Domain, Plan, PlatformStaff, Tenant


class BootstrapPlatformTests(TestCase):
    env = {
        "PUBLIC_DOMAINS": "admin.fivuza.test,fivuza.test",
        "BOOTSTRAP_ADMIN_EMAIL": "Ops@Fivuza.test",
        "BOOTSTRAP_ADMIN_PASSWORD": "una-clave-bien-larga",
    }

    def _run(self, env):
        with mock.patch.dict(os.environ, env, clear=False):
            call_command("bootstrap_platform", stdout=StringIO())

    def test_creates_public_tenant_domains_plans_and_admin(self):
        self._run(self.env)

        public = Tenant.objects.get(schema_name="public")
        self.assertEqual(
            set(Domain.objects.filter(tenant=public).values_list("domain", flat=True)),
            {"admin.fivuza.test", "fivuza.test"},
        )
        self.assertTrue(Domain.objects.get(domain="admin.fivuza.test").is_primary)
        self.assertTrue(Plan.objects.exists())
        staff = PlatformStaff.objects.get(email="ops@fivuza.test")
        self.assertEqual(staff.role, "SUPER_ADMIN")

    def test_is_idempotent(self):
        self._run(self.env)
        self._run(self.env)

        self.assertEqual(Tenant.objects.filter(schema_name="public").count(), 1)
        self.assertEqual(PlatformStaff.objects.count(), 1)

    def test_requires_public_domains(self):
        with self.assertRaises(CommandError):
            self._run({"PUBLIC_DOMAINS": ""})

    def test_rejects_short_admin_password(self):
        env = {**self.env, "BOOTSTRAP_ADMIN_PASSWORD": "corta"}
        with self.assertRaises(CommandError):
            self._run(env)


class InfraHealthCheckTests(TestCase):
    def test_healthz_answers_with_an_unknown_host(self):
        # El healthcheck de Railway no trae el dominio de ningun tenant.
        response = Client(HTTP_HOST="healthcheck.railway.app").get("/healthz")

        self.assertIn(response.status_code, (200, 503))
        self.assertEqual(set(response.json()["checks"]), {"database", "redis"})
