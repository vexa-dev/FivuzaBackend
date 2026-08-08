from datetime import timedelta
from decimal import Decimal

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.db import connection
from django.utils import timezone
from django_tenants.utils import get_tenant_model, schema_context

_REFRESH_DAILY_SALES_SQL = (
    "REFRESH MATERIALIZED VIEW CONCURRENTLY mv_daily_sales_summary;"
)
_REFRESH_LOW_STOCK_SQL = "REFRESH MATERIALIZED VIEW CONCURRENTLY mv_low_stock_alert;"


class DashboardRefreshService:
    """Refresca las 2 vistas materializadas del dashboard (Sprint 24,
    Esquema Backend §9.2). CONCURRENTLY evita bloquear lecturas mientras se
    recalcula -posible gracias al indice unico que cada vista ya tiene
    (migracion 0003_materialized_views). Requiere ejecutarse fuera de una
    transaccion (Postgres no permite REFRESH CONCURRENTLY dentro de un
    BEGIN); Celery no envuelve sus tareas en una transaccion por defecto,
    asi que un cursor directo alcanza."""

    @staticmethod
    def refresh(schema_name: str) -> None:
        with schema_context(schema_name):
            with connection.cursor() as cursor:
                cursor.execute(_REFRESH_DAILY_SALES_SQL)
                cursor.execute(_REFRESH_LOW_STOCK_SQL)

    @staticmethod
    def is_due(schema_name: str) -> bool:
        from core.models import Tenant, TenantSettings
        from dashboard.models import DailySalesSummary

        tenant = Tenant.objects.get(schema_name=schema_name)
        settings_row = TenantSettings.objects.filter(tenant=tenant).first()
        interval_minutes = (
            settings_row.dashboard_refresh_minutes if settings_row else 15
        )

        with schema_context(schema_name):
            last_refresh = (
                DailySalesSummary.objects.order_by("-refreshed_at")
                .values_list("refreshed_at", flat=True)
                .first()
            )
        if last_refresh is None:
            return True
        return timezone.now() - last_refresh >= timedelta(minutes=interval_minutes)

    @staticmethod
    def refresh_due_tenants() -> int:
        """Celery Beat corre esto cada pocos minutos (frecuencia fija, corta)
        y cada tenant decide si ya le toca segun SU PROPIA
        dashboard_refresh_minutes -no hay una entrada de CELERY_BEAT_SCHEDULE
        por tenant, seria imposible de mantener con tenants dinamicos."""
        tenant_model = get_tenant_model()
        refreshed = 0
        for tenant in tenant_model.objects.exclude(schema_name="public"):
            if DashboardRefreshService.is_due(tenant.schema_name):
                DashboardRefreshService.refresh(tenant.schema_name)
                refreshed += 1
        return refreshed


class DashboardMetricsService:
    """Métricas del dashboard (Sprint 24, API Spec §2.4). Ventas de hoy se
    calculan en vivo (la vista materializada puede tener hasta
    dashboard_refresh_minutes de atraso, inaceptable para "hoy"); semana/mes
    y comparativos usan la vista materializada -aceptan ese mismo atraso a
    cambio de no escanear meses de ventas en cada carga del dashboard."""

    @staticmethod
    def sales_today(*, warehouse_id: int | None = None) -> dict:
        from django.db.models import Count, Sum
        from django.db.models.functions import Coalesce

        from ventas.models import Sale

        queryset = Sale.objects.filter(
            status="COMPLETED", created_at__date=timezone.localdate()
        )
        if warehouse_id:
            queryset = queryset.filter(warehouse_id=warehouse_id)
        aggregate = queryset.aggregate(
            total=Coalesce(Sum("total"), Decimal("0")), count=Count("id")
        )
        return {
            "total_sales": str(aggregate["total"]),
            "total_transactions": aggregate["count"],
        }

    @staticmethod
    def sales_range(*, date_from, date_to, warehouse_id: int | None = None) -> dict:
        from dashboard.models import DailySalesSummary

        queryset = DailySalesSummary.objects.filter(
            sale_date__gte=date_from, sale_date__lte=date_to
        )
        if warehouse_id:
            queryset = queryset.filter(warehouse_id=warehouse_id)
        rows = list(queryset.order_by("sale_date").values("sale_date", "total_sales"))
        total = sum((row["total_sales"] for row in rows), Decimal("0"))
        return {
            "total_sales": str(total),
            "by_day": [
                {"date": str(row["sale_date"]), "total": str(row["total_sales"])}
                for row in rows
            ],
        }

    @staticmethod
    def comparison_vs_previous_period(
        *, date_from, date_to, warehouse_id: int | None = None
    ) -> dict:
        period_length = (date_to - date_from).days + 1
        previous_from = date_from - timedelta(days=period_length)
        previous_to = date_from - timedelta(days=1)

        current = DashboardMetricsService.sales_range(
            date_from=date_from, date_to=date_to, warehouse_id=warehouse_id
        )
        previous = DashboardMetricsService.sales_range(
            date_from=previous_from, date_to=previous_to, warehouse_id=warehouse_id
        )
        current_total = Decimal(current["total_sales"])
        previous_total = Decimal(previous["total_sales"])
        change_pct = None
        if previous_total > 0:
            change_pct = str(
                ((current_total - previous_total) / previous_total * 100).quantize(
                    Decimal("0.01")
                )
            )
        return {
            "current_total": current["total_sales"],
            "previous_total": previous["total_sales"],
            "change_pct": change_pct,
        }

    @staticmethod
    def top_products(*, date_from, date_to, limit: int = 5) -> list[dict]:
        from django.db.models import Sum

        from ventas.models import SaleDetail

        rows = (
            SaleDetail.objects.filter(
                sale__status="COMPLETED",
                sale__created_at__date__gte=date_from,
                sale__created_at__date__lte=date_to,
            )
            .values("product_name_snapshot")
            .annotate(quantity_sold=Sum("quantity"), revenue=Sum("subtotal"))
            .order_by("-quantity_sold")[:limit]
        )
        return [
            {
                "product_name": row["product_name_snapshot"],
                "quantity_sold": str(row["quantity_sold"]),
                "revenue": str(row["revenue"]),
            }
            for row in rows
        ]

    @staticmethod
    def gross_margin(*, date_from, date_to) -> dict:
        """Aproximado: SaleDetail no guarda un snapshot del costo al momento
        de la venta (decision documentada -ventas e inventario estan
        desacoplados a proposito, variant_id no es una FK fisica). El margen
        se calcula contra el COSTO ACTUAL de cada variante, no el costo
        historico real de cuando se vendio -si el costo cambio desde
        entonces, el margen de periodos pasados queda ligeramente distorsionado.
        Documentado como limitacion conocida, no como bug."""
        from inventario.models import ProductVariant
        from ventas.models import SaleDetail

        details = SaleDetail.objects.filter(
            sale__status="COMPLETED",
            sale__created_at__date__gte=date_from,
            sale__created_at__date__lte=date_to,
        ).values("variant_id", "quantity", "subtotal")

        costs = dict(ProductVariant.all_objects.values_list("id", "cost"))

        total_revenue = Decimal("0")
        total_cost = Decimal("0")
        for detail in details:
            total_revenue += detail["subtotal"]
            unit_cost = costs.get(detail["variant_id"], Decimal("0"))
            total_cost += unit_cost * detail["quantity"]
        # cost (4 decimales) x quantity (3 decimales) da 7 decimales -se
        # redondea a la misma precision monetaria que el resto del sistema.
        total_cost = total_cost.quantize(Decimal("0.0001"))

        gross_margin_amount = total_revenue - total_cost
        margin_pct = None
        if total_revenue > 0:
            margin_pct = str(
                (gross_margin_amount / total_revenue * 100).quantize(Decimal("0.01"))
            )
        return {
            "total_revenue": str(total_revenue),
            "total_cost": str(total_cost),
            "gross_margin": str(gross_margin_amount),
            "margin_pct": margin_pct,
        }

    @staticmethod
    def critical_stock_count() -> int:
        from dashboard.models import LowStockAlert

        return LowStockAlert.objects.count()

    @staticmethod
    def payment_method_distribution(*, date_from, date_to) -> list[dict]:
        from django.db.models import Sum

        from ventas.models import SalePayment

        rows = (
            SalePayment.objects.filter(
                sale__status="COMPLETED",
                sale__created_at__date__gte=date_from,
                sale__created_at__date__lte=date_to,
            )
            .values("method")
            .annotate(total=Sum("amount"))
            .order_by("-total")
        )
        return [{"method": row["method"], "total": str(row["total"])} for row in rows]


class DashboardBroadcastService:
    """Empuja actualizaciones al dashboard conectado por WebSocket (Sprint
    24, TRD §2.5). Un grupo de Channels por tenant (no por usuario): todos
    los usuarios del mismo negocio conectados ven la misma actualizacion,
    igual que "una venta ocurrio en algun punto de la tienda"."""

    @staticmethod
    def broadcast_sale_completed(
        *, schema_name: str, warehouse_id: int, total: Decimal
    ) -> None:
        channel_layer = get_channel_layer()
        if channel_layer is None:
            return
        async_to_sync(channel_layer.group_send)(
            f"dashboard_{schema_name}",
            {
                "type": "dashboard.event",
                "event": "sale_completed",
                "warehouse_id": warehouse_id,
                "total": str(total),
            },
        )
