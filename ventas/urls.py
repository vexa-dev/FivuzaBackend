from django.urls import path
from rest_framework.routers import DefaultRouter

from ventas import views

router = DefaultRouter()
router.register(
    "ventas/cash-registers", views.CashRegisterViewSet, basename="cash-register"
)
router.register(
    "ventas/cash-sessions", views.CashSessionViewSet, basename="cash-session"
)
router.register(
    "ventas/cash-movements", views.CashMovementViewSet, basename="cash-movement"
)
router.register("ventas/customers", views.CustomerViewSet, basename="customer")
router.register("ventas/promotions", views.PromotionViewSet, basename="promotion")
router.register(
    "ventas/promotion-products",
    views.PromotionProductViewSet,
    basename="promotion-product",
)
router.register("ventas/sales", views.SaleViewSet, basename="sale")
router.register("ventas/sale-returns", views.SaleReturnViewSet, basename="sale-return")
router.register(
    "ventas/customer-debt-ledger",
    views.CustomerDebtLedgerViewSet,
    basename="customer-debt-ledger",
)
router.register(
    "ventas/customer-balance-ledger",
    views.CustomerBalanceLedgerViewSet,
    basename="customer-balance-ledger",
)

urlpatterns = [
    path(
        "ventas/cash-sessions/open/",
        views.CashSessionOpenView.as_view(),
        name="cash-session-open",
    ),
    path(
        "ventas/cash-sessions/<int:pk>/close/",
        views.CashSessionCloseView.as_view(),
        name="cash-session-close",
    ),
    path(
        "ventas/pos/catalog/",
        views.POSCatalogView.as_view(),
        name="pos-catalog",
    ),
    path(
        "ventas/pos/search/",
        views.POSSearchView.as_view(),
        name="pos-search",
    ),
]

# El router va al final: open/ debe resolverse antes de que el patron de
# detalle del router (ventas/cash-sessions/<pk>/) intente tomar "open" como
# si fuera un pk.
urlpatterns += router.urls
