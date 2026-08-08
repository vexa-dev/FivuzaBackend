from rest_framework import viewsets
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core.permissions import TenantNotCanceled, TenantNotSuspended
from dashboard.models import DashboardWidget
from dashboard.serializers import DashboardWidgetSerializer
from dashboard.services import DashboardMetricsService

_DASHBOARD_PERMISSIONS = [IsAuthenticated, TenantNotSuspended, TenantNotCanceled]


class DashboardWidgetViewSet(viewsets.ModelViewSet):
    """Configuración de qué widgets ve cada usuario y en qué orden (Sprint
    24). Cada usuario solo ve/edita sus propios widgets -no hay ningún caso
    de uso donde alguien configure el dashboard de otro."""

    serializer_class = DashboardWidgetSerializer
    permission_classes = _DASHBOARD_PERMISSIONS

    def get_queryset(self):
        return DashboardWidget.objects.filter(user=self.request.user).order_by(
            "position"
        )

    def perform_create(self, serializer):
        serializer.save(user=self.request.user)


class DashboardMetricsView(APIView):
    """GET /dashboard/metrics/?warehouse= (Sprint 24, API Spec §2.4). Una
    sola respuesta agregada en vez de N endpoints -el dashboard siempre
    pinta todo junto, separarlo solo multiplicaria round-trips."""

    permission_classes = _DASHBOARD_PERMISSIONS

    def get(self, request):
        warehouse_id = request.query_params.get("warehouse")
        warehouse_id = int(warehouse_id) if warehouse_id else None

        return Response(
            DashboardMetricsService.get_all_metrics(warehouse_id=warehouse_id)
        )
