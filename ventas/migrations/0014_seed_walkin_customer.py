from django.db import migrations


def seed_walkin_customer(apps, schema_editor):
    """Bloque D.1: TenantProvisioningService.seed_default_resources() solo
    corre al nacer un tenant -esta migracion siembra el cliente de paso en
    los tenants existentes (mismo motivo que las migraciones de datos de
    permisos: es una migracion de tenant, django-tenants la corre contra el
    esquema de cada uno). document_number fijo porque Customer.document_number
    es unico -no puede depender de nada variable del tenant."""
    Customer = apps.get_model("ventas", "Customer")
    Customer.objects.get_or_create(
        document_type="ANONIMO",
        document_number="00000000",
        defaults={"name": "Cliente de paso", "is_walk_in": True, "is_active": True},
    )


class Migration(migrations.Migration):
    dependencies = [
        ("ventas", "0013_paymentsettlement_paymentsettlementline_and_more"),
    ]

    operations = [
        migrations.RunPython(seed_walkin_customer, migrations.RunPython.noop),
    ]
