from django.urls import path
from rest_framework.routers import DefaultRouter

from inventario import views

router = DefaultRouter()
router.register("inventario/warehouses", views.WarehouseViewSet, basename="warehouse")
router.register("inventario/categories", views.CategoryViewSet, basename="category")
router.register("inventario/suppliers", views.SupplierViewSet, basename="supplier")
router.register("inventario/attributes", views.AttributeViewSet, basename="attribute")
router.register(
    "inventario/attribute-values",
    views.AttributeValueViewSet,
    basename="attribute-value",
)
router.register("inventario/products", views.ProductViewSet, basename="product")
router.register(
    "inventario/product-variants",
    views.ProductVariantViewSet,
    basename="product-variant",
)
router.register("inventario/stock", views.StockViewSet, basename="stock")
router.register(
    "inventario/inventory-movements",
    views.InventoryMovementViewSet,
    basename="inventory-movement",
)
router.register("inventario/tax-rates", views.TaxRateViewSet, basename="tax-rate")
router.register(
    "inventario/product-taxes", views.ProductTaxViewSet, basename="product-tax"
)
router.register(
    "inventario/purchase-orders",
    views.PurchaseOrderViewSet,
    basename="purchase-order",
)

urlpatterns = [
    path(
        "inventario/stock/adjust/",
        views.StockAdjustView.as_view(),
        name="stock-adjust",
    ),
    path(
        "inventario/stock/low-stock/",
        views.LowStockVariantsView.as_view(),
        name="stock-low-stock",
    ),
]

urlpatterns += router.urls
