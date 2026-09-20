"""Crea desde cero el tenant de las pruebas E2E (Playwright, FivuzaFrontend).

Borra y recrea el esquema `e2e` en cada corrida para que las pruebas partan
siempre del mismo estado: un admin, dos cajeros con su caja asignada (Bloque
A), la caja por defecto, un producto con stock y un cliente. Solo corre con
DEBUG=True o E2E_SEED_ALLOWED=True: borra un esquema completo y nunca debe
ejecutarse contra produccion.

    python manage.py seed_e2e
"""

import os
from decimal import Decimal

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from django_tenants.utils import schema_context

from core.models import Domain, Tenant, TenantSettings
from core.services import TenantProvisioningService

E2E_SCHEMA = "e2e"
E2E_DOMAIN = "e2e.localhost"
E2E_ADMIN_EMAIL = "admin@e2e.fivuza.test"
E2E_ADMIN_PASSWORD = "Clave-E2E-2026"
# Bloque A: dos cajeros, cada uno con su caja asignada, para probar que no
# se pisan entre si (una sesion por caja, arqueo a ciegas y entrega).
E2E_CASHIER_ONE_EMAIL = "cajero1@e2e.fivuza.test"
E2E_CASHIER_TWO_EMAIL = "cajero2@e2e.fivuza.test"
E2E_CASHIER_PASSWORD = "Clave-E2E-2026"
E2E_CASHIER_ONE_REGISTER = "Caja Cajero 1"
E2E_CASHIER_TWO_REGISTER = "Caja Cajero 2"
E2E_PRODUCT_NAME = "Camiseta E2E"
E2E_PRODUCT_SKU = "E2E-001"
E2E_PRODUCT_PRICE = "20.00"
E2E_CUSTOMER_NAME = "Cliente E2E"
E2E_CUSTOMER_DOCUMENT = "70000001"


class Command(BaseCommand):
    help = "Recrea el tenant 'e2e' con datos fijos para las pruebas de Playwright."

    def handle(self, *args, **options):
        if not (settings.DEBUG or os.getenv("E2E_SEED_ALLOWED") == "True"):
            raise CommandError(
                "seed_e2e borra un esquema completo: solo corre con DEBUG=True "
                "o E2E_SEED_ALLOWED=True."
            )

        existing = Tenant.objects.filter(schema_name=E2E_SCHEMA).first()
        if existing is not None:
            # Las relaciones de core hacia Tenant son PROTECT: se borran
            # primero sus filas (settings, dominios, notas, etc.).
            for relation in Tenant._meta.related_objects:
                relation.related_model.objects.filter(
                    **{relation.field.name: existing}
                ).delete()
            # IF EXISTS: tolera un intento anterior que alcanzo a borrar el
            # esquema pero no la fila.
            with connection.cursor() as cursor:
                cursor.execute(f'DROP SCHEMA IF EXISTS "{E2E_SCHEMA}" CASCADE')
            existing.delete(force_drop=False)
            self.stdout.write("Tenant e2e anterior eliminado.")

        tenant = Tenant(schema_name=E2E_SCHEMA, company_name="Negocio E2E")
        tenant.save()
        Domain.objects.create(domain=E2E_DOMAIN, tenant=tenant, is_primary=True)
        # Sincronico a proposito: en un entorno de pruebas no se depende de
        # que el worker de Celery ya haya sembrado roles, almacen y caja.
        TenantProvisioningService.seed_default_roles(tenant)
        TenantProvisioningService.seed_default_resources(tenant)

        TenantSettings.objects.filter(tenant=tenant).update(
            cashier_can_open_session=True, cashier_can_close_session=True
        )

        with schema_context(E2E_SCHEMA):
            self._seed_business_data()

        self.stdout.write(
            self.style.SUCCESS(
                f"Tenant e2e listo en {E2E_DOMAIN} (usuario {E2E_ADMIN_EMAIL})."
            )
        )

    def _seed_business_data(self):
        from inventario.models import Category, Warehouse
        from inventario.services import ProductVariantService, StockService
        from usuarios.models import Role, User, UserWarehouse
        from ventas.models import CashRegister, Customer

        admin = User.objects.create(
            email=E2E_ADMIN_EMAIL, role=Role.objects.get(name="admin")
        )
        admin.set_password(E2E_ADMIN_PASSWORD)
        admin.save()

        warehouse = Warehouse.objects.get(name="Principal")
        seller_role = Role.objects.get(name="seller")
        for email, register_name in (
            (E2E_CASHIER_ONE_EMAIL, E2E_CASHIER_ONE_REGISTER),
            (E2E_CASHIER_TWO_EMAIL, E2E_CASHIER_TWO_REGISTER),
        ):
            cashier = User.objects.create(email=email, role=seller_role)
            cashier.set_password(E2E_CASHIER_PASSWORD)
            cashier.save()
            UserWarehouse.objects.create(user=cashier, warehouse=warehouse)
            CashRegister.objects.create(
                warehouse=warehouse, name=register_name, assigned_user=cashier
            )

        category = Category.objects.create(name="Ropa")
        product = ProductVariantService.create_product(
            product_data={
                "type": "PRODUCT",
                "name": E2E_PRODUCT_NAME,
                "category": category,
                "unit_of_measure": "UND",
            },
            variants_data=[{"sku": E2E_PRODUCT_SKU, "price": E2E_PRODUCT_PRICE}],
        )
        StockService.adjust_stock(
            variant=product.variants.first(),
            warehouse=warehouse,
            counted_quantity=Decimal("100"),
            concept="ADJUSTMENT",
            user=admin,
        )
        Customer.objects.create(
            document_type="DNI",
            document_number=E2E_CUSTOMER_DOCUMENT,
            name=E2E_CUSTOMER_NAME,
        )
