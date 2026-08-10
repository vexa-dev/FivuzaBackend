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

urlpatterns = router.urls
