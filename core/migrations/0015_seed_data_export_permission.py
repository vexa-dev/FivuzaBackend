from django.db import migrations


def seed_data_export_permission(apps, schema_editor):
    """Sprint 33: DATA_EXPORT es un permiso nuevo, pero _seed_default_roles()
    solo corre una vez, al nacer un tenant (señal post_schema_sync). Los
    tenants aprovisionados antes de este sprint no lo reciben salvo que se
    re-siembre a mano aqui (mismo patron ya usado en las migraciones 0009 y
    0012)."""
    from core.services import TenantProvisioningService

    Tenant = apps.get_model("core", "Tenant")
    for tenant in Tenant.objects.exclude(schema_name="public"):
        TenantProvisioningService.seed_default_roles(tenant)


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0014_tenant_terms_accepted_at_and_more"),
    ]

    operations = [
        migrations.RunPython(seed_data_export_permission, migrations.RunPython.noop),
    ]
