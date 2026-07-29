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

urlpatterns = router.urls
