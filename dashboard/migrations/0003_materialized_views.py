from django.db import migrations

CREATE_DAILY_SALES_SUMMARY = """
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

DROP_DAILY_SALES_SUMMARY = "DROP MATERIALIZED VIEW IF EXISTS mv_daily_sales_summary;"

CREATE_LOW_STOCK_ALERT = """
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

DROP_LOW_STOCK_ALERT = "DROP MATERIALIZED VIEW IF EXISTS mv_low_stock_alert;"


class Migration(migrations.Migration):
    """Crea las 2 vistas materializadas documentadas (MEJORA 2): se refrescan periódicamente
    (ej. REFRESH MATERIALIZED VIEW CONCURRENTLY, cada 15-30 min vía celery beat) en vez de
    calcularse en cada carga del dashboard. Los índices únicos habilitan el refresh CONCURRENTLY."""

    dependencies = [
        ("dashboard", "0002_initial"),
        ("ventas", "0001_initial"),
        ("inventario", "0002_initial"),
    ]

    operations = [
        migrations.RunSQL(
            sql=CREATE_DAILY_SALES_SUMMARY, reverse_sql=DROP_DAILY_SALES_SUMMARY
        ),
        migrations.RunSQL(sql=CREATE_LOW_STOCK_ALERT, reverse_sql=DROP_LOW_STOCK_ALERT),
    ]
