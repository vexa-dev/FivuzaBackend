"""Deja listo el esquema public de un entorno nuevo (staging/produccion).

Reemplaza a poc_tenant.py fuera de desarrollo: es idempotente (se puede
correr en cada deploy) y toma todo de variables de entorno, sin datos de
prueba.

    PUBLIC_DOMAINS=admin.fivuza.com,fivuza.com
    BOOTSTRAP_ADMIN_EMAIL=ops@fivuza.com
    BOOTSTRAP_ADMIN_PASSWORD=...   (solo la primera vez; luego puede borrarse)
"""

import os

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from core.models import Domain, PlatformStaff, Tenant


class Command(BaseCommand):
    help = "Crea (si faltan) el tenant public, sus dominios, los planes y el primer SUPER_ADMIN."

    def handle(self, *args, **options):
        domains = [
            d.strip().lower()
            for d in os.getenv("PUBLIC_DOMAINS", "").split(",")
            if d.strip()
        ]
        if not domains:
            raise CommandError("Define PUBLIC_DOMAINS (dominios del panel interno).")

        with transaction.atomic():
            public, created = Tenant.objects.get_or_create(
                schema_name="public",
                defaults={"company_name": "Fivuza (plataforma)"},
            )
            self.stdout.write(f"Tenant public {'creado' if created else 'ya existia'}.")
            for index, domain in enumerate(domains):
                _, created = Domain.objects.get_or_create(
                    domain=domain,
                    defaults={"tenant": public, "is_primary": index == 0},
                )
                if created:
                    self.stdout.write(f"Dominio {domain} registrado.")

        call_command("seed_plans")
        self._ensure_super_admin()

    def _ensure_super_admin(self):
        email = os.getenv("BOOTSTRAP_ADMIN_EMAIL", "").strip().lower()
        if not email:
            self.stdout.write("BOOTSTRAP_ADMIN_EMAIL vacio: no se crea administrador.")
            return
        if PlatformStaff.objects.filter(email=email).exists():
            self.stdout.write(f"El administrador {email} ya existe.")
            return
        password = os.getenv("BOOTSTRAP_ADMIN_PASSWORD", "")
        if len(password) < 12:
            raise CommandError(
                "BOOTSTRAP_ADMIN_PASSWORD debe tener al menos 12 caracteres."
            )
        staff = PlatformStaff(
            email=email, full_name="Administrador Fivuza", role="SUPER_ADMIN"
        )
        staff.set_password(password)
        staff.save()
        self.stdout.write(
            f"SUPER_ADMIN {email} creado. Borra BOOTSTRAP_ADMIN_PASSWORD del entorno."
        )
