from django.db import models
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core.permissions import RequiresFeature, TenantNotCanceled, TenantNotSuspended
from inventario.models import Warehouse
from usuarios.permissions import HasModulePermission
from ventas.models import (
    CashMovement,
    CashRegister,
    CashSession,
    Customer,
    Promotion,
    PromotionProduct,
    Sale,
)
from ventas.serializers import (
    CashMovementReceiptUploadURLSerializer,
    CashMovementSerializer,
    CashRegisterSerializer,
    CashSessionCloseSerializer,
    CashSessionDetailSerializer,
    CashSessionOpenSerializer,
    CashSessionSerializer,
    CustomerSerializer,
    PromotionProductSerializer,
    PromotionSerializer,
    SaleCreateSerializer,
    SaleSerializer,
)
from ventas.services import POSCatalogService

# Lectura: cualquier tenant.users autenticado con el modulo de caja activo
# (Especificacion de API §2.3: sin permiso de escritura listado = "-" =
# abierto a lectura). Escritura: solo CASH_MANAGE.
_CASH_READ_PERMISSIONS = [
    IsAuthenticated,
    TenantNotSuspended,
    TenantNotCanceled,
    RequiresFeature("HAS_CASH_MODULE"),
]
_CASH_WRITE_PERMISSIONS = [
    IsAuthenticated,
    TenantNotSuspended,
    TenantNotCanceled,
    RequiresFeature("HAS_CASH_MODULE"),
    HasModulePermission("CASH_MANAGE"),
]

# Mismo esquema lectura/escritura que Caja (Sprint 12): lectura abierta a
# cualquier tenant.users autenticado con el modulo de ventas activo,
# escritura requiere SALES_MANAGE.
_SALES_READ_PERMISSIONS = [
    IsAuthenticated,
    TenantNotSuspended,
    TenantNotCanceled,
    RequiresFeature("HAS_SALES_MODULE"),
]
_SALES_WRITE_PERMISSIONS = [
    IsAuthenticated,
    TenantNotSuspended,
    TenantNotCanceled,
    RequiresFeature("HAS_SALES_MODULE"),
    HasModulePermission("SALES_MANAGE"),
]


class CashRegisterViewSet(viewsets.ModelViewSet):
    queryset = CashRegister.objects.all().order_by("name")
    serializer_class = CashRegisterSerializer

    def get_permissions(self):
        if self.action in ("list", "retrieve"):
            return [permission() for permission in _CASH_READ_PERMISSIONS]
        return [permission() for permission in _CASH_WRITE_PERMISSIONS]


class CashSessionViewSet(
    mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet
):
    """Solo lectura -abrir/cerrar son acciones propias (CashSessionOpenView/
    CashSessionCloseView), no un create/update generico (Especificacion de
    API §4.4)."""

    queryset = CashSession.objects.all().order_by("-opening_at")
    serializer_class = CashSessionSerializer
    permission_classes = _CASH_READ_PERMISSIONS

    def get_serializer_class(self):
        if self.action == "retrieve":
            return CashSessionDetailSerializer
        return CashSessionSerializer

    def get_queryset(self):
        queryset = super().get_queryset()
        params = self.request.query_params
        cash_register_id = params.get("cash_register")
        if cash_register_id:
            queryset = queryset.filter(cash_register_id=cash_register_id)
        status_param = params.get("status")
        if status_param:
            queryset = queryset.filter(status=status_param)
        user_id = params.get("user")
        if user_id:
            queryset = queryset.filter(user_id=user_id)
        opening_from = params.get("opening_from")
        if opening_from:
            queryset = queryset.filter(opening_at__date__gte=opening_from)
        opening_to = params.get("opening_to")
        if opening_to:
            queryset = queryset.filter(opening_at__date__lte=opening_to)
        return queryset


class CashMovementViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    viewsets.GenericViewSet,
):
    """Sin update/destroy: un movimiento de caja es un registro de auditoria
    -se corrige con un movimiento de ajuste nuevo, nunca editando/borrando
    el original (mismo principio que PlatformAuditLog/AuditLog)."""

    queryset = CashMovement.objects.all().order_by("-created_at")
    serializer_class = CashMovementSerializer

    def get_permissions(self):
        if self.action in ("list", "retrieve"):
            return [permission() for permission in _CASH_READ_PERMISSIONS]
        return [permission() for permission in _CASH_WRITE_PERMISSIONS]

    def get_queryset(self):
        queryset = super().get_queryset()
        session_id = self.request.query_params.get("cash_session")
        if session_id:
            queryset = queryset.filter(cash_session_id=session_id)
        return queryset

    @action(detail=False, methods=["post"], url_path="upload-receipt-url")
    def upload_receipt_url(self, request):
        """No es detail-route: un CashMovement no tiene id todavia cuando se
        pide la URL de subida del comprobante (a diferencia de
        ProductVariantViewSet.upload_image_url, que si opera sobre un objeto
        ya existente)."""
        serializer = CashMovementReceiptUploadURLSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return Response(serializer.save())


class CashSessionOpenView(APIView):
    """POST -> abre una sesion de caja (Especificacion de API §4.4)."""

    permission_classes = _CASH_WRITE_PERMISSIONS

    def post(self, request):
        serializer = CashSessionOpenSerializer(
            data=request.data, context={"request": request}
        )
        serializer.is_valid(raise_exception=True)
        session = serializer.save()
        return Response(
            CashSessionSerializer(session).data, status=status.HTTP_201_CREATED
        )


class CashSessionCloseView(APIView):
    """POST -> cierra una sesion de caja con arqueo (Especificacion de API
    §4.4): calcula expected_closing_amount y guarda la diferencia contra lo
    contado."""

    permission_classes = _CASH_WRITE_PERMISSIONS

    def post(self, request, pk):
        session = get_object_or_404(CashSession, pk=pk)
        serializer = CashSessionCloseSerializer(
            data=request.data, context={"request": request, "session": session}
        )
        serializer.is_valid(raise_exception=True)
        session = serializer.save()
        return Response(CashSessionSerializer(session).data)


class CustomerViewSet(viewsets.ModelViewSet):
    """Sin ActiveManager (Customer no hereda SoftDeleteModel -es preexistente
    a la BDD v5, no un modelo nuevo de este sprint): el filtrado de bajas se
    hace a mano en get_queryset(), mismo resultado que Warehouse/Category."""

    queryset = Customer.objects.all().order_by("name")
    serializer_class = CustomerSerializer

    def get_permissions(self):
        if self.action in ("list", "retrieve"):
            return [permission() for permission in _SALES_READ_PERMISSIONS]
        return [permission() for permission in _SALES_WRITE_PERMISSIONS]

    def get_queryset(self):
        queryset = super().get_queryset().filter(deleted_at__isnull=True)
        search = self.request.query_params.get("search")
        if search:
            queryset = queryset.filter(
                models.Q(document_number__icontains=search)
                | models.Q(name__icontains=search)
                | models.Q(phone__icontains=search)
            )
        return queryset

    def perform_destroy(self, instance):
        instance.deleted_at = timezone.now()
        instance.deleted_by = self.request.user
        instance.is_active = False
        instance.save(update_fields=["deleted_at", "deleted_by", "is_active"])


class PromotionViewSet(viewsets.ModelViewSet):
    queryset = Promotion.objects.all().order_by("-start_date")
    serializer_class = PromotionSerializer

    def get_permissions(self):
        if self.action in ("list", "retrieve"):
            return [permission() for permission in _SALES_READ_PERMISSIONS]
        return [permission() for permission in _SALES_WRITE_PERMISSIONS]

    def get_queryset(self):
        queryset = super().get_queryset()
        search = self.request.query_params.get("search")
        if search:
            queryset = queryset.filter(name__icontains=search)
        return queryset


class PromotionProductViewSet(viewsets.ModelViewSet):
    queryset = PromotionProduct.objects.all()
    serializer_class = PromotionProductSerializer
    permission_classes = _SALES_WRITE_PERMISSIONS

    def get_queryset(self):
        queryset = super().get_queryset()
        promotion_id = self.request.query_params.get("promotion")
        if promotion_id:
            queryset = queryset.filter(promotion_id=promotion_id)
        return queryset


class SaleViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    viewsets.GenericViewSet,
):
    """Sin update/destroy: una venta es un registro de auditoria -se corrige
    con una devolucion (SaleReturn, sprint posterior), nunca editando o
    borrando la venta original (mismo principio que CashMovement).
    create() usa un serializer distinto de list/retrieve (SaleCreateSerializer
    no es un ModelSerializer -delega toda la validacion de negocio en
    SaleService.create_sale(), Especificacion de API §4.1)."""

    queryset = Sale.objects.all().order_by("-created_at")
    serializer_class = SaleSerializer

    def get_serializer_class(self):
        if self.action == "create":
            return SaleCreateSerializer
        return SaleSerializer

    def get_permissions(self):
        if self.action in ("list", "retrieve"):
            return [permission() for permission in _SALES_READ_PERMISSIONS]
        return [permission() for permission in _SALES_WRITE_PERMISSIONS]

    def get_queryset(self):
        queryset = super().get_queryset()
        params = self.request.query_params
        customer_id = params.get("customer")
        if customer_id:
            queryset = queryset.filter(customer_id=customer_id)
        user_id = params.get("user")
        if user_id:
            queryset = queryset.filter(user_id=user_id)
        cash_register_id = params.get("cash_register")
        if cash_register_id:
            queryset = queryset.filter(cash_session__cash_register_id=cash_register_id)
        date_from = params.get("date_from")
        if date_from:
            queryset = queryset.filter(created_at__date__gte=date_from)
        date_to = params.get("date_to")
        if date_to:
            queryset = queryset.filter(created_at__date__lte=date_to)
        return queryset

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        sale = serializer.save()
        return Response(SaleSerializer(sale).data, status=status.HTTP_201_CREATED)


class POSCatalogView(APIView):
    """GET -> catalogo completo optimizado para el POS (Sprint 16, Esquema
    Backend §6.2): payload reducido pensado para cachearse en el cliente,
    base del futuro modo offline."""

    permission_classes = _SALES_READ_PERMISSIONS

    def get(self, request):
        warehouse = get_object_or_404(
            Warehouse, pk=request.query_params.get("warehouse")
        )
        return Response(POSCatalogService.catalog(warehouse=warehouse))


class POSSearchView(APIView):
    """GET -> busqueda del POS con prioridad de escaneo (Sprint 16): primero
    coincidencia exacta por barcode, y solo si falla, busqueda difusa por
    nombre/sku. Un escaneo debe resolver de inmediato, sin competir con
    resultados de texto parecidos."""

    permission_classes = _SALES_READ_PERMISSIONS

    def get(self, request):
        warehouse = get_object_or_404(
            Warehouse, pk=request.query_params.get("warehouse")
        )
        query = request.query_params.get("q", "").strip()
        if not query:
            return Response([])
        return Response(POSCatalogService.search(warehouse=warehouse, query=query))
