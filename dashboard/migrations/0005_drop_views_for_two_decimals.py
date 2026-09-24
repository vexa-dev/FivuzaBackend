from importlib import import_module

from django.db import migrations

# Postgres no deja cambiar el tipo de una columna que usa una vista
# ("cannot alter type of a column used by a view or rule"): las vistas
# materializadas del dashboard leen sales.total/discount_total y
# stock.quantity/product_variants.min_stock, que pasan a 2 decimales. Se
# borran aqui, antes de alterar esas columnas, y 0006 las recrea igual que
# estaban (0003 y 0004).
CREATE_VIEWS = import_module(
    "dashboard.migrations.0006_recreate_views_two_decimals"
).CREATE_VIEWS

DROP_VIEWS = """
DROP MATERIALIZED VIEW IF EXISTS mv_daily_sales_summary;
DROP MATERIALIZED VIEW IF EXISTS mv_low_stock_alert;
"""


class Migration(migrations.Migration):
    dependencies = [
        ("dashboard", "0004_daily_sales_local_date"),
    ]

    run_before = [
        ("ventas", "0012_dos_decimales"),
        ("inventario", "0013_dos_decimales"),
    ]

    # Al revertir: 0006 las borra, las columnas vuelven a 3/4 decimales y
    # aqui se recrean sobre ellas.
    operations = [
        migrations.RunSQL(sql=DROP_VIEWS, reverse_sql=CREATE_VIEWS),
    ]
