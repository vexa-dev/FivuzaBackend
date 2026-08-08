from django.urls import path
from rest_framework.routers import DefaultRouter

from dashboard import views

router = DefaultRouter()
router.register(
    "dashboard/widgets", views.DashboardWidgetViewSet, basename="dashboard-widget"
)

urlpatterns = [
    path(
        "dashboard/metrics/",
        views.DashboardMetricsView.as_view(),
        name="dashboard_metrics",
    ),
]

urlpatterns += router.urls
