from django.db import migrations


def seed_gym_manage_permission(apps, schema_editor):
    """Sprint 29: GYM_MANAGE es un permiso nuevo, pero _seed_default_roles()
    solo corre una vez, al nacer un tenant (señal post_schema_sync). Los
    tenants aprovisionados antes de este sprint no lo reciben salvo que se
    re-siembre a mano aqui (mismo patron ya usado en la migracion 0009 para
    SALES_VOID/SALES_RETURN)."""
    from core.services import TenantProvisioningService

    Tenant = apps.get_model("core", "Tenant")
    for tenant in Tenant.objects.exclude(schema_name="public"):
        TenantProvisioningService.seed_default_roles(tenant)


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0011_tenantsettings_gym_module_enabled_and_more"),
    ]

    operations = [
        migrations.RunPython(seed_gym_manage_permission, migrations.RunPython.noop),
    ]
