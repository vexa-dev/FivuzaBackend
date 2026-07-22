from django.urls import path
from rest_framework.routers import DefaultRouter
from rest_framework_simplejwt.views import TokenRefreshView

from core import views

router = DefaultRouter()
router.register("core/tenants", views.TenantViewSet, basename="tenant")
router.register("core/plans", views.PlanViewSet, basename="plan")
router.register("core/plan-features", views.PlanFeatureViewSet, basename="plan-feature")
router.register(
    "core/subscriptions", views.SubscriptionViewSet, basename="subscription"
)
router.register(
    "core/subscription-payments",
    views.SubscriptionPaymentViewSet,
    basename="subscription-payment",
)
router.register(
    "core/tenant-settings", views.TenantSettingsViewSet, basename="tenant-settings"
)
router.register(
    "core/platform-staff", views.PlatformStaffViewSet, basename="platform-staff"
)

urlpatterns = [
    # Especificacion de API, seccion 3.2: /api/v1/platform/auth/... (no
    # /api/v1/auth/platform/...) -refresh/logout no estan explicitos en el
    # doc, se ubican junto a login por simetria con la seccion 3.1
    # (tenant.users).
    path(
        "platform/auth/login/",
        views.PlatformStaffLoginView.as_view(),
        name="platform_login",
    ),
    path("platform/auth/refresh/", TokenRefreshView.as_view(), name="platform_refresh"),
    path(
        "platform/auth/logout/",
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

# El router va al final: suspend/reactivate deben resolverse antes de que el
# patron de detalle del router (core/tenants/<pk>/) intente tomar "suspend"
# o "reactivate" como si fueran un pk.
urlpatterns += router.urls
