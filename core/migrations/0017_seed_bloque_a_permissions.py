from django.db import migrations


def seed_bloque_a_permissions(apps, schema_editor):
    """Bloque A: CASH_OPEN, CASH_CLOSE, INVENTORY_VIEW_COST y SETTINGS_MANAGE
    son permisos nuevos, pero _seed_default_roles() solo corre una vez, al
    nacer un tenant (señal post_schema_sync). Los tenants aprovisionados
    antes de este bloque no los reciben salvo que se re-siembre a mano aqui
    (mismo patron ya usado en las migraciones 0009, 0012 y 0015)."""
    from core.services import TenantProvisioningService

    Tenant = apps.get_model("core", "Tenant")
    for tenant in Tenant.objects.exclude(schema_name="public"):
        TenantProvisioningService.seed_default_roles(tenant)


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0016_tenantsettings_cashier_switches"),
    ]

    operations = [
        migrations.RunPython(seed_bloque_a_permissions, migrations.RunPython.noop),
    ]
