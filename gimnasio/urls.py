from rest_framework.routers import DefaultRouter

from gimnasio import views

router = DefaultRouter()
router.register(
    "gimnasio/membership-plans", views.MembershipPlanViewSet, basename="membership-plan"
)
router.register("gimnasio/memberships", views.MembershipViewSet, basename="membership")

urlpatterns = router.urls
