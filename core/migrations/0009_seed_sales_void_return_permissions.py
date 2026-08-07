from django.db import migrations


def seed_sales_void_return_permissions(apps, schema_editor):
    """Sprint 18: SALES_VOID/SALES_RETURN son permisos nuevos, pero
    _seed_default_roles() solo corre una vez, al nacer un tenant (señal
    post_schema_sync). Los tenants aprovisionados antes de este sprint no
    los reciben salvo que se re-siembren a mano aqui (misma limitacion ya
    documentada para SALES_MANAGE desde el Sprint 12)."""
    from core.services import TenantProvisioningService

    Tenant = apps.get_model("core", "Tenant")
    for tenant in Tenant.objects.exclude(schema_name="public"):
        TenantProvisioningService.seed_default_roles(tenant)


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0008_tenantsettings_cash_difference_alert_threshold"),
    ]

    operations = [
        migrations.RunPython(
            seed_sales_void_return_permissions, migrations.RunPython.noop
        ),
    ]
