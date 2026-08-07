from django.core.exceptions import ValidationError as DjangoValidationError
from django.http import HttpResponse
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.parsers import MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core.permissions import RequiresFeature, TenantNotCanceled, TenantNotSuspended
from core.services import FeatureFlagService
from inventario import selectors
from inventario.models import (
    Attribute,
    AttributeValue,
    Category,
    InventoryMovement,
    Product,
    ProductTax,
    ProductVariant,
    PurchaseOrder,
    Stock,
    Supplier,
    TaxRate,
    Warehouse,
)
from inventario.permissions import HasInventoryAccess
from inventario.serializers import (
    AttributeSerializer,
    AttributeValueSerializer,
    CategorySerializer,
    InventoryMovementSerializer,
    LowStockVariantSerializer,
    ProductSerializer,
    ProductTaxSerializer,
    ProductVariantImageUploadURLSerializer,
    ProductVariantSerializer,
    PurchaseOrderSerializer,
    StockAdjustSerializer,
    StockSerializer,
    SupplierSerializer,
    TaxRateSerializer,
    WarehouseSerializer,
)
from usuarios.permissions import HasModulePermission

_BASE_PERMISSIONS = [
    IsAuthenticated,
    TenantNotSuspended,
    TenantNotCanceled,
    HasInventoryAccess,
]
_PURCHASES_PERMISSIONS = [
    IsAuthenticated,
    TenantNotSuspended,
    TenantNotCanceled,
    RequiresFeature("HAS_PURCHASES_MODULE"),
    HasModulePermission("PURCHASES_MANAGE"),
]


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


class StockViewSet(viewsets.ReadOnlyModelViewSet):
    """Solo lectura -el saldo de Stock nunca se edita directo, siempre pasa
    por StockService.adjust_stock() via StockAdjustView (Esquema Backend §5.2)."""

    queryset = Stock.objects.select_related("variant", "warehouse")
    serializer_class = StockSerializer
    permission_classes = _BASE_PERMISSIONS

    def get_queryset(self):
        queryset = super().get_queryset()
        variant_id = self.request.query_params.get("variant")
        if variant_id:
            queryset = queryset.filter(variant_id=variant_id)
        warehouse_id = self.request.query_params.get("warehouse")
        if warehouse_id:
            queryset = queryset.filter(warehouse_id=warehouse_id)
        return queryset


class InventoryMovementViewSet(viewsets.ReadOnlyModelViewSet):
    """Kardex -solo lectura, filtrable por variante/almacen/rango de fechas
    (API Spec §4.6). Se escribe unicamente via StockService.

    Sprint 21: `oversell_only=true` filtra a los movimientos que una venta
    offline registro pese a no haber stock suficiente (oversell_flag=True,
    Sprint 20) -es el "reporte" que el dueño usa para revisar y ajustar
    inventario tras un episodio sin conexion. No se creo un endpoint nuevo:
    es el mismo Kardex, con un filtro mas, igual que variant/warehouse."""

    queryset = InventoryMovement.objects.select_related("variant", "warehouse", "user")
    serializer_class = InventoryMovementSerializer
    permission_classes = _BASE_PERMISSIONS

    def get_queryset(self):
        queryset = super().get_queryset().order_by("-created_at")
        variant_id = self.request.query_params.get("variant")
        if variant_id:
            queryset = queryset.filter(variant_id=variant_id)
        warehouse_id = self.request.query_params.get("warehouse")
        if warehouse_id:
            queryset = queryset.filter(warehouse_id=warehouse_id)
        date_from = self.request.query_params.get("date_from")
        if date_from:
            queryset = queryset.filter(created_at__date__gte=date_from)
        date_to = self.request.query_params.get("date_to")
        if date_to:
            queryset = queryset.filter(created_at__date__lte=date_to)
        if self.request.query_params.get("oversell_only") == "true":
            queryset = queryset.filter(oversell_flag=True)
        return queryset


class StockAdjustView(APIView):
    """POST conteo fisico/merma/ajuste -unico punto de entrada HTTP hacia
    StockService.adjust_stock() (API Spec §4.6)."""

    permission_classes = [
        IsAuthenticated,
        TenantNotSuspended,
        TenantNotCanceled,
        HasInventoryAccess,
    ]

    def post(self, request):
        serializer = StockAdjustSerializer(
            data=request.data, context={"request": request}
        )
        serializer.is_valid(raise_exception=True)
        try:
            movement = serializer.save()
        except DjangoValidationError as exc:
            raise ValidationError(exc.message) from exc
        return Response(
            InventoryMovementSerializer(movement).data, status=status.HTTP_201_CREATED
        )


class LowStockVariantsView(APIView):
    """GET listado de variantes por debajo de su min_stock -usado por el
    badge de alertas del layout (PRD, perfil 'Dueño')."""

    permission_classes = _BASE_PERMISSIONS

    def get(self, request):
        variants = selectors.get_low_stock_variants()
        return Response(LowStockVariantSerializer(variants, many=True).data)


class TaxRateViewSet(viewsets.ModelViewSet):
    queryset = TaxRate.objects.all().order_by("name")
    serializer_class = TaxRateSerializer
    permission_classes = _PURCHASES_PERMISSIONS


class ProductTaxViewSet(viewsets.ModelViewSet):
    queryset = ProductTax.objects.select_related("variant", "tax_rate")
    serializer_class = ProductTaxSerializer
    permission_classes = _PURCHASES_PERMISSIONS

    def get_queryset(self):
        queryset = super().get_queryset()
        variant_id = self.request.query_params.get("variant")
        if variant_id:
            queryset = queryset.filter(variant_id=variant_id)
        return queryset


class PurchaseOrderViewSet(viewsets.ModelViewSet):
    """Visible solo si tenant_settings.purchases_enabled=true
    (RequiresFeature). La recepcion pasa siempre por PurchaseService, nunca
    por un PATCH directo de `status` -por eso `status` es de solo lectura
    en el serializer."""

    queryset = PurchaseOrder.objects.select_related(
        "supplier", "warehouse", "user"
    ).prefetch_related("details")
    serializer_class = PurchaseOrderSerializer
    permission_classes = _PURCHASES_PERMISSIONS

    def get_queryset(self):
        queryset = super().get_queryset().order_by("-created_at")
        status_param = self.request.query_params.get("status")
        if status_param:
            queryset = queryset.filter(status=status_param)
        supplier_id = self.request.query_params.get("supplier")
        if supplier_id:
            queryset = queryset.filter(supplier_id=supplier_id)
        return queryset

    @action(detail=True, methods=["post"])
    def receive(self, request, pk=None):
        from inventario.services import PurchaseService

        order = self.get_object()
        try:
            order = PurchaseService.receive_order(
                purchase_order=order, user=request.user
            )
        except DjangoValidationError as exc:
            raise ValidationError(exc.message) from exc

        movements_created = order.details.count()
        return Response(
            {
                "id": order.id,
                "status": order.status,
                "movements_created": movements_created,
            },
            status=status.HTTP_200_OK,
        )


class CatalogImportTemplateView(APIView):
    """GET plantilla CSV descargable para la importacion masiva (Sprint 6,
    hueco #3)."""

    permission_classes = _BASE_PERMISSIONS

    def get(self, request):
        from inventario.services import CatalogImportService

        response = HttpResponse(
            CatalogImportService.build_template_csv(), content_type="text/csv"
        )
        response["Content-Disposition"] = (
            'attachment; filename="plantilla_catalogo.csv"'
        )
        return response


class CatalogImportView(APIView):
    """POST archivo CSV -valida fila por fila y confirma solo las validas,
    con reporte de errores (Sprint 6, hueco #3)."""

    permission_classes = _BASE_PERMISSIONS
    parser_classes = [MultiPartParser]

    def post(self, request):
        from inventario.services import CatalogImportService

        uploaded_file = request.FILES.get("file")
        if not uploaded_file:
            raise ValidationError({"file": "Este campo es requerido."})

        try:
            content = uploaded_file.read().decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ValidationError(
                {"file": "El archivo debe ser un CSV codificado en UTF-8."}
            ) from exc

        try:
            report = CatalogImportService.import_csv(
                file_content=content, user=request.user
            )
        except DjangoValidationError as exc:
            raise ValidationError(exc.message) from exc

        return Response(report, status=status.HTTP_200_OK)
