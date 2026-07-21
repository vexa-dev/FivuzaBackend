from django.db import models

from usuarios.models import User


class DashboardWidget(models.Model):
    """Configuración opcional de widgets visibles por usuario; omitible si el dashboard es fijo."""

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="dashboard_widgets")
    widget_code = models.CharField(
        max_length=30,
        choices=[
            ("SALES_TODAY", "SALES_TODAY"),
            ("LOW_STOCK", "LOW_STOCK"),
            ("CASH_STATUS", "CASH_STATUS"),
            ("TOP_PRODUCTS", "TOP_PRODUCTS"),
            ("ATTENDANCE_TODAY", "ATTENDANCE_TODAY"),
        ],
    )
    position = models.IntegerField()
    is_visible = models.BooleanField(default=True)

    class Meta:
        db_table = "dashboard_widgets"


class DailySalesSummary(models.Model):
    """Vista materializada mv_daily_sales_summary. No gestionada por Django (managed=False);
    se crea/refresca con SQL nativo en dashboard/migrations/0003_materialized_views.py.
    id es sintético (row_number), ya que la llave natural es (sale_date, warehouse_id)."""

    id = models.IntegerField(primary_key=True)
    sale_date = models.DateField()
    warehouse_id = models.IntegerField()
    total_sales = models.DecimalField(max_digits=14, decimal_places=4)
    total_transactions = models.IntegerField()
    total_discount = models.DecimalField(max_digits=14, decimal_places=4)
    refreshed_at = models.DateTimeField()

    class Meta:
        managed = False
        db_table = "mv_daily_sales_summary"


class LowStockAlert(models.Model):
    """Vista materializada mv_low_stock_alert. No gestionada por Django (managed=False);
    se crea/refresca con SQL nativo en dashboard/migrations/0003_materialized_views.py.
    id es sintético (row_number), ya que la llave natural es (variant_id, warehouse_id)."""

    id = models.IntegerField(primary_key=True)
    variant_id = models.IntegerField()
    warehouse_id = models.IntegerField()
    current_quantity = models.DecimalField(max_digits=12, decimal_places=3)
    min_stock = models.DecimalField(max_digits=12, decimal_places=3)
    refreshed_at = models.DateTimeField()

    class Meta:
        managed = False
        db_table = "mv_low_stock_alert"
