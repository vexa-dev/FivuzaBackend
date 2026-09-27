from django.db import migrations


def seed_bloque_d_permissions(apps, schema_editor):
    """Bloque D: SALES_CASH_REFUND (D.5) y SALES_RECONCILE (D.6) son nuevos
    y seed_default_roles() solo corre al nacer un tenant -mismo motivo que
    0008_seed_sales_discount_permission.py para sembrarlos aqui."""
    Permission = apps.get_model("usuarios", "Permission")
    Role = apps.get_model("usuarios", "Role")
    RolePermission = apps.get_model("usuarios", "RolePermission")

    permissions = {
        code: Permission.objects.get_or_create(code=code, defaults={"module": "SALES"})[0]
        for code in ("SALES_CASH_REFUND", "SALES_RECONCILE")
    }
    for role in Role.objects.filter(
        name__in=("admin", "manager"), is_system_default=True
    ):
        for permission in permissions.values():
            RolePermission.objects.get_or_create(role=role, permission=permission)


class Migration(migrations.Migration):
    dependencies = [
        ("usuarios", "0009_dos_decimales"),
    ]

    operations = [
        migrations.RunPython(seed_bloque_d_permissions, migrations.RunPython.noop),
    ]
