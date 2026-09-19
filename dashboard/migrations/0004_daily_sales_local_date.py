from django.db import migrations

# Mismo esquema que 0003, pero el dia se calcula en la hora del negocio
# (America/Lima) y con la hora real de la venta (occurred_at). Antes,
# DATE(created_at) usaba UTC: las ventas despues de las 19:00 en Peru caian
# en el dia siguiente, y una venta offline quedaba en el dia en que se
# sincronizo.
CREATE_DAILY_SALES_SUMMARY = """
DROP MATERIALIZED VIEW IF EXISTS mv_daily_sales_summary;

CREATE MATERIALIZED VIEW mv_daily_sales_summary AS
SELECT
    row_number() OVER () AS id,
    (s.occurred_at AT TIME ZONE 'America/Lima')::date AS sale_date,
    s.warehouse_id AS warehouse_id,
    COALESCE(SUM(s.total), 0) AS total_sales,
    COUNT(*) AS total_transactions,
    COALESCE(SUM(s.discount_total), 0) AS total_discount,
    now() AS refreshed_at
FROM sales s
WHERE s.status = 'COMPLETED'
GROUP BY (s.occurred_at AT TIME ZONE 'America/Lima')::date, s.warehouse_id
WITH DATA;

CREATE UNIQUE INDEX mv_daily_sales_summary_date_wh_idx
    ON mv_daily_sales_summary (sale_date, warehouse_id);
"""

RESTORE_UTC_DAILY_SALES_SUMMARY = """
DROP MATERIALIZED VIEW IF EXISTS mv_daily_sales_summary;

CREATE MATERIALIZED VIEW mv_daily_sales_summary AS
SELECT
    row_number() OVER () AS id,
    DATE(s.created_at) AS sale_date,
    s.warehouse_id AS warehouse_id,
    COALESCE(SUM(s.total), 0) AS total_sales,
    COUNT(*) AS total_transactions,
    COALESCE(SUM(s.discount_total), 0) AS total_discount,
    now() AS refreshed_at
FROM sales s
WHERE s.status = 'COMPLETED'
GROUP BY DATE(s.created_at), s.warehouse_id
WITH DATA;

CREATE UNIQUE INDEX mv_daily_sales_summary_date_wh_idx
    ON mv_daily_sales_summary (sale_date, warehouse_id);
"""


class Migration(migrations.Migration):
    dependencies = [
        ("dashboard", "0003_materialized_views"),
        ("ventas", "0008_sale_occurred_at"),
    ]

    operations = [
        migrations.RunSQL(
            sql=CREATE_DAILY_SALES_SUMMARY,
            reverse_sql=RESTORE_UTC_DAILY_SALES_SUMMARY,
        ),
    ]
