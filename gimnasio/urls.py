from django.urls import path
from rest_framework.routers import DefaultRouter

from gimnasio import views

router = DefaultRouter()
router.register(
    "gimnasio/membership-plans", views.MembershipPlanViewSet, basename="membership-plan"
)
router.register("gimnasio/memberships", views.MembershipViewSet, basename="membership")
router.register("gimnasio/classes", views.GymClassViewSet, basename="gym-class")
router.register(
    "gimnasio/class-schedules", views.ClassScheduleViewSet, basename="class-schedule"
)
router.register(
    "gimnasio/class-bookings", views.ClassBookingViewSet, basename="class-booking"
)
router.register(
    "gimnasio/membership-groups",
    views.MembershipGroupViewSet,
    basename="membership-group",
)

urlpatterns = router.urls + [
    path("gimnasio/check-in/", views.CheckInView.as_view(), name="gym-check-in"),
    path(
        "gimnasio/reports/class-attendance/",
        views.ClassAttendanceReportView.as_view(),
        name="gym-report-class-attendance",
    ),
    path(
        "gimnasio/reports/memberships-expiring/",
        views.MembershipsExpiringReportView.as_view(),
        name="gym-report-memberships-expiring",
    ),
    path(
        "gimnasio/reports/revenue-by-plan/",
        views.RevenueByPlanReportView.as_view(),
        name="gym-report-revenue-by-plan",
    ),
]
