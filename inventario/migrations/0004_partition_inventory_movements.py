from django.db import migrations

from core.partitioning import bootstrap_initial_partitions

# inventory_movements (Kardex) se particiona por RANGE sobre created_at
# desde este sprint, antes de que StockService empiece a escribir en ella
# de verdad -reparticionar despues, con movimientos ya acumulados, seria
# una migracion mucho mas cara (TRD §4.4; Esquema Backend §5.2).
_CONVERT_TO_PARTITIONED_SQL = """
ALTER TABLE inventory_movements RENAME TO inventory_movements_legacy;

CREATE TABLE inventory_movements (
    id serial NOT NULL,
    variant_id integer NOT NULL,
    warehouse_id integer NOT NULL,
    user_id integer NOT NULL,
    type varchar(3) NOT NULL,
    quantity numeric(12,3) NOT NULL,
    concept varchar(20) NOT NULL,
    reference_id integer NULL,
    oversell_flag boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_inventory_movements_type CHECK (type IN ('IN', 'OUT')),
    CONSTRAINT fk_inventory_movements_variant FOREIGN KEY (variant_id) REFERENCES product_variants(id),
    CONSTRAINT fk_inventory_movements_warehouse FOREIGN KEY (warehouse_id) REFERENCES warehouses(id),
    CONSTRAINT fk_inventory_movements_user FOREIGN KEY (user_id) REFERENCES users(id)
) PARTITION BY RANGE (created_at);

CREATE INDEX inventory_movements_variant_id_idx ON inventory_movements (variant_id);
CREATE INDEX inventory_movements_warehouse_id_idx ON inventory_movements (warehouse_id);
"""

_COPY_AND_DROP_LEGACY_SQL = """
INSERT INTO inventory_movements (
    id, variant_id, warehouse_id, user_id, type, quantity, concept,
    reference_id, oversell_flag, created_at
)
SELECT
    id, variant_id, warehouse_id, user_id, type, quantity, concept,
    reference_id, oversell_flag, created_at
FROM inventory_movements_legacy;

SELECT setval(
    pg_get_serial_sequence('inventory_movements', 'id'),
    COALESCE((SELECT MAX(id) FROM inventory_movements), 1)
);

DROP TABLE inventory_movements_legacy;
"""

_REVERT_SQL = """
ALTER TABLE inventory_movements RENAME TO inventory_movements_partitioned;
CREATE TABLE inventory_movements (
    id serial PRIMARY KEY,
    variant_id integer NOT NULL REFERENCES product_variants(id),
    warehouse_id integer NOT NULL REFERENCES warehouses(id),
    user_id integer NOT NULL REFERENCES users(id),
    type varchar(3) NOT NULL CHECK (type IN ('IN', 'OUT')),
    quantity numeric(12,3) NOT NULL,
    concept varchar(20) NOT NULL,
    reference_id integer NULL,
    oversell_flag boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now()
);
INSERT INTO inventory_movements (
    id, variant_id, warehouse_id, user_id, type, quantity, concept,
    reference_id, oversell_flag, created_at
)
SELECT
    id, variant_id, warehouse_id, user_id, type, quantity, concept,
    reference_id, oversell_flag, created_at
FROM inventory_movements_partitioned;
DROP TABLE inventory_movements_partitioned CASCADE;
"""


def bootstrap_partitions(apps, schema_editor):
    bootstrap_initial_partitions("inventory_movements")


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("inventario", "0003_productvariant_barcode_and_more"),
    ]

    operations = [
        migrations.RunSQL(
            sql=_CONVERT_TO_PARTITIONED_SQL,
            reverse_sql=_REVERT_SQL,
            state_operations=[],
        ),
        # Las particiones (incluida la 'default') deben existir ANTES de
        # copiar filas -si no, el INSERT falla porque Postgres no acepta
        # escrituras en una tabla particionada sin una particion que
        # reciba esa fecha.
        migrations.RunPython(bootstrap_partitions, noop_reverse),
        migrations.RunSQL(
            sql=_COPY_AND_DROP_LEGACY_SQL,
            reverse_sql=migrations.RunSQL.noop,
            state_operations=[],
        ),
    ]
