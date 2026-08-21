from django.db import migrations


def backfill_user_warehouses(apps, schema_editor):
    User = apps.get_model("usuarios", "User")
    UserWarehouse = apps.get_model("usuarios", "UserWarehouse")
    Warehouse = apps.get_model("inventario", "Warehouse")

    warehouse_ids = list(
        Warehouse.objects.filter(is_active=True, deleted_at__isnull=True).values_list(
            "id", flat=True
        )
    )
    if not warehouse_ids:
        return

    users = (
        User.objects.filter(is_active=True, deleted_at__isnull=True)
        .exclude(role__name__iexact="admin", role__is_system_default=True)
        .exclude(warehouse_access__isnull=False)
        .values_list("id", flat=True)
    )
    UserWarehouse.objects.bulk_create(
        [
            UserWarehouse(user_id=user_id, warehouse_id=warehouse_id)
            for user_id in users
            for warehouse_id in warehouse_ids
        ],
        ignore_conflicts=True,
        batch_size=500,
    )


class Migration(migrations.Migration):
    dependencies = [
        ("inventario", "0011_category_allowed_attributes"),
        ("usuarios", "0005_alter_permission_module_dataexport"),
    ]

    operations = [
        migrations.RunPython(backfill_user_warehouses, migrations.RunPython.noop)
    ]
