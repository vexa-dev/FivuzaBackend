from django.urls import path
from rest_framework_simplejwt.views import TokenRefreshView

from core import views

urlpatterns = [
    path(
        "auth/platform/login/",
        views.PlatformStaffLoginView.as_view(),
        name="platform_login",
    ),
    path("auth/platform/refresh/", TokenRefreshView.as_view(), name="platform_refresh"),
    path(
        "auth/platform/logout/",
        views.PlatformStaffLogoutView.as_view(),
        name="platform_logout",
    ),
    path(
        "core/tenants/<int:pk>/suspend/",
        views.TenantSuspendView.as_view(),
        name="tenant_suspend",
    ),
    path(
        "core/tenants/<int:pk>/reactivate/",
        views.TenantReactivateView.as_view(),
        name="tenant_reactivate",
    ),
]
