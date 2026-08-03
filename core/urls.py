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
router.register(
    "core/platform-audit-logs",
    views.PlatformAuditLogViewSet,
    basename="platform-audit-log",
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
        "core/tenants/register/",
        views.TenantRegisterView.as_view(),
        name="tenant_register",
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
    path(
        "core/tenants/<int:pk>/cancel/",
        views.TenantCancelView.as_view(),
        name="tenant_cancel",
    ),
    path(
        "core/subscription-payments/<int:pk>/confirm/",
        views.SubscriptionPaymentConfirmView.as_view(),
        name="subscription_payment_confirm",
    ),
    path(
        "core/tenants/<int:pk>/impersonation/",
        views.TenantImpersonationStartView.as_view(),
        name="tenant_impersonation_start",
    ),
    path(
        "core/tenants/<int:pk>/impersonation/<int:session_id>/",
        views.TenantImpersonationEndView.as_view(),
        name="tenant_impersonation_end",
    ),
    path(
        "core/tenants/<int:pk>/feature-overrides/",
        views.TenantFeatureOverrideListView.as_view(),
        name="tenant_feature_overrides",
    ),
    path(
        "core/tenants/<int:pk>/feature-overrides/<str:feature_code>/",
        views.TenantFeatureOverrideView.as_view(),
        name="tenant_feature_override",
    ),
    # Fuera de /core/: lo llama el ERP del tenant (autenticado con un token
    # de tenant.users, no de platform_staff) para terminar su propia sesion
    # de soporte desde el banner ("Salir").
    path(
        "impersonation/end/",
        views.ImpersonationSelfEndView.as_view(),
        name="impersonation_self_end",
    ),
    path(
        "core/dashboard/summary/",
        views.DashboardSummaryView.as_view(),
        name="dashboard_summary",
    ),
    path(
        "core/tenants/<int:pk>/notes/",
        views.TenantNoteListCreateView.as_view(),
        name="tenant_notes",
    ),
    path(
        "core/subscription-discounts/",
        views.SubscriptionDiscountListCreateView.as_view(),
        name="subscription_discounts",
    ),
    path(
        "core/subscription-discounts/<int:pk>/",
        views.SubscriptionDiscountDetailView.as_view(),
        name="subscription_discount_detail",
    ),
    path(
        "core/tenants/<int:pk>/onboarding/",
        views.TenantOnboardingView.as_view(),
        name="tenant_onboarding",
    ),
    path(
        "core/tenants/<int:pk>/health/",
        views.TenantHealthView.as_view(),
        name="tenant_health",
    ),
    path(
        "core/tenants/<int:pk>/consumption/",
        views.TenantConsumptionView.as_view(),
        name="tenant_consumption",
    ),
]

# El router va al final: suspend/reactivate/cancel/confirm deben resolverse
# antes de que el patron de detalle del router (core/tenants/<pk>/,
# core/subscription-payments/<pk>/) intente tomarlos como si fueran un pk.
urlpatterns += router.urls
