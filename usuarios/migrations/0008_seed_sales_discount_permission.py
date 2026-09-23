from django.db import migrations


def seed_sales_discount_permission(apps, schema_editor):
    """Bloque C.2: SALES_DISCOUNT es nuevo y seed_default_roles() solo corre
    al nacer un tenant. Va como migracion del tenant (no de core, como
    0009/0012/0015/0017) porque sembrar desde el esquema public leeria Role
    antes de que el tenant tenga la columna max_discount_percent (0007)."""
    Permission = apps.get_model("usuarios", "Permission")
    Role = apps.get_model("usuarios", "Role")
    RolePermission = apps.get_model("usuarios", "RolePermission")

    permission, _ = Permission.objects.get_or_create(
        code="SALES_DISCOUNT", defaults={"module": "SALES"}
    )
    for role in Role.objects.filter(
        name__in=("admin", "manager"), is_system_default=True
    ):
        RolePermission.objects.get_or_create(role=role, permission=permission)


class Migration(migrations.Migration):
    dependencies = [
        ("usuarios", "0007_supervisor_authorization_login_attempts"),
    ]

    operations = [
        migrations.RunPython(seed_sales_discount_permission, migrations.RunPython.noop),
    ]
