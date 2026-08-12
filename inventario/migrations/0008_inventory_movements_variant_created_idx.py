from django.db import migrations

# Sprint 34: el patron de acceso real del Kardex (StockService, reportes) es
# "variante X, ordenado/filtrado por fecha" -el indice simple de variant_id
# (migracion 0004) no cubre el filtro de created_at, asi que Postgres lo
# aplica como un escaneo dentro de cada particion. CREATE INDEX sobre la
# tabla particionada crea automaticamente el indice equivalente en todas las
# particiones existentes y en las que se creen despues.
_CREATE_INDEX_SQL = """
CREATE INDEX inventory_movements_variant_created_idx
ON inventory_movements (variant_id, created_at);
"""

_DROP_INDEX_SQL = """
DROP INDEX IF EXISTS inventory_movements_variant_created_idx;
"""


class Migration(migrations.Migration):
    dependencies = [
        (
            "inventario",
            "0007_alter_inventorymovement_concept_volumepricingtier_and_more",
        ),
    ]

    operations = [
        migrations.RunSQL(
            sql=_CREATE_INDEX_SQL,
            reverse_sql=_DROP_INDEX_SQL,
            state_operations=[],
        ),
    ]
