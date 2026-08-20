from django.urls import path
from rest_framework.routers import DefaultRouter

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
router.register(
    "usuarios/user-warehouses", views.UserWarehouseViewSet, basename="user-warehouse"
)
router.register("usuarios/audit-logs", views.AuditLogViewSet, basename="audit-log")
router.register("usuarios/employees", views.EmployeeViewSet, basename="employee")
router.register(
    "usuarios/employee-schedules",
    views.EmployeeScheduleViewSet,
    basename="employee-schedule",
)
router.register(
    "usuarios/employee-attendance",
    views.EmployeeAttendanceViewSet,
    basename="employee-attendance",
)
router.register(
    "usuarios/employee-payroll",
    views.EmployeePayrollViewSet,
    basename="employee-payroll",
)
router.register(
    "usuarios/data-exports", views.DataExportViewSet, basename="data-export"
)

urlpatterns = [
    # Especificacion de API, seccion 3.1: /api/v1/auth/... (flujo de
    # tenant.users, distinto de /api/v1/platform/auth/... para platform_staff).
    path("auth/login/", views.TenantUserLoginView.as_view(), name="tenant_user_login"),
    path(
        "auth/refresh/",
        views.TenantUserRefreshView.as_view(),
        name="tenant_user_refresh",
    ),
    path(
        "auth/logout/", views.TenantUserLogoutView.as_view(), name="tenant_user_logout"
    ),
    path(
        "auth/password-reset/",
        views.PasswordResetRequestView.as_view(),
        name="password_reset_request",
    ),
    path(
        "auth/password-reset/confirm/",
        views.PasswordResetConfirmView.as_view(),
        name="password_reset_confirm",
    ),
    # Reportes de RRHH (Sprint 23, API Spec §2.1) -APIView, no ViewSet: no
    # representan un recurso CRUD, son agregados de solo lectura.
    path(
        "usuarios/reports/attendance/",
        views.AttendanceReportView.as_view(),
        name="attendance_report",
    ),
    path(
        "usuarios/reports/payroll-cost/",
        views.PayrollCostReportView.as_view(),
        name="payroll_cost_report",
    ),
    # Derechos ARCO (Sprint 33, Ley N 29733).
    path(
        "usuarios/me/data-export/",
        views.OwnDataExportView.as_view(),
        name="own_data_export",
    ),
    path(
        "usuarios/users/<int:pk>/anonymize/",
        views.UserAnonymizeView.as_view(),
        name="user_anonymize",
    ),
]

urlpatterns += router.urls
