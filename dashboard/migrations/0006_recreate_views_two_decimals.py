from django.db import migrations

# Recrea las vistas que 0005 borro para poder pasar las columnas a 2
# decimales: mismas definiciones que dejaron 0003 (stock bajo) y 0004
# (ventas por dia en la hora del negocio).
CREATE_VIEWS = """
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

CREATE MATERIALIZED VIEW mv_low_stock_alert AS
SELECT
    row_number() OVER () AS id,
    st.variant_id AS variant_id,
    st.warehouse_id AS warehouse_id,
    st.quantity AS current_quantity,
    pv.min_stock AS min_stock,
    now() AS refreshed_at
FROM stock st
JOIN product_variants pv ON pv.id = st.variant_id
WHERE st.quantity < pv.min_stock
WITH DATA;

CREATE UNIQUE INDEX mv_low_stock_alert_variant_wh_idx
    ON mv_low_stock_alert (variant_id, warehouse_id);
"""

DROP_VIEWS = """
DROP MATERIALIZED VIEW IF EXISTS mv_daily_sales_summary;
DROP MATERIALIZED VIEW IF EXISTS mv_low_stock_alert;
"""


class Migration(migrations.Migration):
    dependencies = [
        ("dashboard", "0005_drop_views_for_two_decimals"),
        ("ventas", "0012_dos_decimales"),
        ("inventario", "0013_dos_decimales"),
    ]

    operations = [
        migrations.RunSQL(sql=CREATE_VIEWS, reverse_sql=DROP_VIEWS),
    ]
