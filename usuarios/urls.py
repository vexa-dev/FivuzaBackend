from django.urls import path
from rest_framework.routers import DefaultRouter
from rest_framework_simplejwt.views import TokenRefreshView

from usuarios import views

router = DefaultRouter()
router.register("usuarios/roles", views.RoleViewSet, basename="role")
router.register("usuarios/permissions", views.PermissionViewSet, basename="permission")
router.register(
    "usuarios/role-permissions", views.RolePermissionViewSet, basename="role-permission"
)
router.register(
    "usuarios/role-permissions-history",
    views.RolePermissionsHistoryViewSet,
    basename="role-permission-history",
)
router.register("usuarios/users", views.UserViewSet, basename="user")
router.register(
    "usuarios/user-permissions", views.UserPermissionViewSet, basename="user-permission"
)
router.register("usuarios/audit-logs", views.AuditLogViewSet, basename="audit-log")

urlpatterns = [
    # Especificacion de API, seccion 3.1: /api/v1/auth/... (flujo de
    # tenant.users, distinto de /api/v1/platform/auth/... para platform_staff).
    path("auth/login/", views.TenantUserLoginView.as_view(), name="tenant_user_login"),
    path("auth/refresh/", TokenRefreshView.as_view(), name="tenant_user_refresh"),
    path(
        "auth/logout/", views.TenantUserLogoutView.as_view(), name="tenant_user_logout"
    ),
]

urlpatterns += router.urls
