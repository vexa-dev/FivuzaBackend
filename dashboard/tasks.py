"""Tareas de Celery propias de dashboard.

DashboardRefreshService: ejecuta REFRESH MATERIALIZED VIEW CONCURRENTLY sobre
mv_daily_sales_summary y mv_low_stock_alert cada 15-30 minutos vía Celery Beat.
Nunca se invoca desde un request HTTP directo.
"""
