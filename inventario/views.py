from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from core.permissions import TenantNotSuspended
from core.services import FeatureFlagService
from inventario.models import (
    Attribute,
    AttributeValue,
    Category,
    Product,
    ProductVariant,
    Supplier,
    Warehouse,
)
from inventario.permissions import HasInventoryAccess
from inventario.serializers import (
    AttributeSerializer,
    AttributeValueSerializer,
    CategorySerializer,
    ProductSerializer,
    ProductVariantImageUploadURLSerializer,
    ProductVariantSerializer,
    SupplierSerializer,
    WarehouseSerializer,
)

_BASE_PERMISSIONS = [IsAuthenticated, TenantNotSuspended, HasInventoryAccess]


class WarehouseViewSet(viewsets.ModelViewSet):
    queryset = Warehouse.objects.all().order_by("name")
    serializer_class = WarehouseSerializer
    permission_classes = _BASE_PERMISSIONS

    def get_queryset(self):
        queryset = super().get_queryset()
        search = self.request.query_params.get("search")
        if search:
            queryset = queryset.filter(name__icontains=search)
        return queryset

    def perform_create(self, serializer):
        multi_warehouse = FeatureFlagService.is_enabled(
            self.request.tenant, "HAS_MULTI_WAREHOUSE"
        )
        if not multi_warehouse and Warehouse.objects.exists():
            raise PermissionDenied(
                {
                    "code": "MODULE_DISABLED",
                    "feature": "HAS_MULTI_WAREHOUSE",
                    "message": "El plan/configuracion actual solo permite 1 almacen.",
                }
            )
        serializer.save()

    def perform_destroy(self, instance):
        instance.deleted_at = timezone.now()
        instance.deleted_by = self.request.user
        instance.is_active = False
        instance.save(update_fields=["deleted_at", "deleted_by", "is_active"])


class CategoryViewSet(viewsets.ModelViewSet):
    queryset = Category.objects.all().order_by("name")
    serializer_class = CategorySerializer
    permission_classes = _BASE_PERMISSIONS

    def get_queryset(self):
        queryset = super().get_queryset()
        search = self.request.query_params.get("search")
        if search:
            queryset = queryset.filter(name__icontains=search)
        return queryset

    def perform_destroy(self, instance):
        instance.deleted_at = timezone.now()
        instance.deleted_by = self.request.user
        instance.is_active = False
        instance.save(update_fields=["deleted_at", "deleted_by", "is_active"])


class SupplierViewSet(viewsets.ModelViewSet):
    queryset = Supplier.objects.all().order_by("company_name")
    serializer_class = SupplierSerializer
    permission_classes = _BASE_PERMISSIONS

    def get_queryset(self):
        queryset = super().get_queryset()
        search = self.request.query_params.get("search")
        if search:
            queryset = queryset.filter(company_name__icontains=search)
        return queryset

    def perform_destroy(self, instance):
        instance.deleted_at = timezone.now()
        instance.deleted_by = self.request.user
        instance.save(update_fields=["deleted_at", "deleted_by"])


class AttributeViewSet(viewsets.ModelViewSet):
    queryset = Attribute.objects.all().order_by("name")
    serializer_class = AttributeSerializer
    permission_classes = _BASE_PERMISSIONS


class AttributeValueViewSet(viewsets.ModelViewSet):
    queryset = AttributeValue.objects.all().order_by("value")
    serializer_class = AttributeValueSerializer
    permission_classes = _BASE_PERMISSIONS


class ProductViewSet(viewsets.ModelViewSet):
    queryset = Product.objects.select_related("category", "supplier").prefetch_related(
        "variants"
    )
    serializer_class = ProductSerializer
    permission_classes = _BASE_PERMISSIONS

    def get_queryset(self):
        queryset = super().get_queryset().order_by("name")
        search = self.request.query_params.get("search")
        if search:
            queryset = queryset.filter(name__icontains=search)
        category_id = self.request.query_params.get("category")
        if category_id:
            queryset = queryset.filter(category_id=category_id)
        return queryset

    def perform_create(self, serializer):
        variants_input = serializer.validated_data.get("variants_input") or []
        if len(variants_input) > 1 and not FeatureFlagService.is_enabled(
            self.request.tenant, "HAS_VARIANTS"
        ):
            raise PermissionDenied(
                {
                    "code": "MODULE_DISABLED",
                    "feature": "HAS_VARIANTS",
                    "message": "El plan/configuracion actual no permite variantes.",
                }
            )
        serializer.save()

    def perform_destroy(self, instance):
        instance.deleted_at = timezone.now()
        instance.deleted_by = self.request.user
        instance.is_active = False
        instance.save(update_fields=["deleted_at", "deleted_by", "is_active"])


class ProductVariantViewSet(viewsets.ModelViewSet):
    queryset = ProductVariant.objects.select_related("product").prefetch_related(
        "attribute_values"
    )
    serializer_class = ProductVariantSerializer
    permission_classes = _BASE_PERMISSIONS

    def get_queryset(self):
        queryset = super().get_queryset().order_by("sku")
        search = self.request.query_params.get("search")
        if search:
            queryset = queryset.filter(sku__icontains=search)
        product_id = self.request.query_params.get("product")
        if product_id:
            queryset = queryset.filter(product_id=product_id)
        barcode = self.request.query_params.get("barcode")
        if barcode:
            queryset = queryset.filter(barcode=barcode)
        return queryset

    def perform_destroy(self, instance):
        sibling_count = (
            ProductVariant.objects.filter(product_id=instance.product_id)
            .exclude(pk=instance.pk)
            .count()
        )
        if sibling_count == 0:
            raise ValidationError(
                "No se puede eliminar la unica variante de un producto."
            )
        was_default = instance.is_default
        instance.deleted_at = timezone.now()
        instance.deleted_by = self.request.user
        instance.is_active = False
        instance.save(update_fields=["deleted_at", "deleted_by", "is_active"])
        if was_default:
            next_default = ProductVariant.objects.filter(
                product_id=instance.product_id
            ).first()
            if next_default:
                next_default.is_default = True
                next_default.save(update_fields=["is_default"])

    @action(detail=True, methods=["post"], url_path="upload-image-url")
    def upload_image_url(self, request, pk=None):
        variant = self.get_object()
        serializer = ProductVariantImageUploadURLSerializer(
            data=request.data, context={"variant": variant}
        )
        serializer.is_valid(raise_exception=True)
        result = serializer.save()
        return Response(result, status=status.HTTP_200_OK)
