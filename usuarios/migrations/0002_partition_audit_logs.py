from django.db import migrations

from core.partitioning import bootstrap_initial_partitions

# audit_logs ya existia como tabla normal desde la migracion 0001 (Sprint 2).
# Postgres no permite convertir una tabla existente en particionada con
# ALTER TABLE -hay que recrearla. Es seguro hacerlo ahora (antes de tener
# negocios piloto en produccion con datos reales) via rename+create+copy;
# hacerlo despues, con auditoria ya acumulada, seria una migracion mucho
# mas cara (Esquema Backend §4.2, §4.3; BDD v5, mejora 1).
_CONVERT_TO_PARTITIONED_SQL = """
ALTER TABLE audit_logs RENAME TO audit_logs_legacy;

CREATE TABLE audit_logs (
    id serial NOT NULL,
    user_id integer NOT NULL,
    action varchar(100) NOT NULL,
    entity varchar(100) NOT NULL,
    entity_id integer NOT NULL,
    details text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT fk_audit_logs_user FOREIGN KEY (user_id) REFERENCES users(id)
) PARTITION BY RANGE (created_at);

CREATE INDEX audit_logs_user_id_idx ON audit_logs (user_id);
"""

_COPY_AND_DROP_LEGACY_SQL = """
INSERT INTO audit_logs (id, user_id, action, entity, entity_id, details, created_at)
SELECT id, user_id, action, entity, entity_id, details, created_at FROM audit_logs_legacy;

SELECT setval(
    pg_get_serial_sequence('audit_logs', 'id'),
    COALESCE((SELECT MAX(id) FROM audit_logs), 1)
);

DROP TABLE audit_logs_legacy;
"""

_REVERT_SQL = """
ALTER TABLE audit_logs RENAME TO audit_logs_partitioned;
CREATE TABLE audit_logs (
    id serial PRIMARY KEY,
    user_id integer NOT NULL REFERENCES users(id),
    action varchar(100) NOT NULL,
    entity varchar(100) NOT NULL,
    entity_id integer NOT NULL,
    details text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now()
);
INSERT INTO audit_logs (id, user_id, action, entity, entity_id, details, created_at)
SELECT id, user_id, action, entity, entity_id, details, created_at FROM audit_logs_partitioned;
DROP TABLE audit_logs_partitioned CASCADE;
"""


def bootstrap_partitions(apps, schema_editor):
    bootstrap_initial_partitions("audit_logs")


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("usuarios", "0001_initial"),
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
