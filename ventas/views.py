from django.db import models
from django.http import HttpResponse
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
    CustomerBalanceLedger,
    CustomerDebtLedger,
    Promotion,
    PromotionProduct,
    Sale,
    SaleReturn,
)
from ventas.serializers import (
    CashMovementReceiptUploadURLSerializer,
    CashMovementSerializer,
    CashRegisterSerializer,
    CashSessionCloseSerializer,
    CashSessionDetailSerializer,
    CashSessionOpenSerializer,
    CashSessionSerializer,
    CustomerBalanceLedgerSerializer,
    CustomerDebtLedgerSerializer,
    CustomerSerializer,
    PromotionProductSerializer,
    PromotionSerializer,
    RegisterDebtPaymentSerializer,
    SaleCreateSerializer,
    SaleReturnCreateSerializer,
    SaleReturnSerializer,
    SaleSerializer,
    SaleSyncSerializer,
    SaleVoidSerializer,
)
from ventas.services import (
    CreditLedgerService,
    POSCatalogService,
    ReceiptService,
    SaleNotFoundError,
)

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
# Separados de SALES_MANAGE a proposito (Sprint 18): un cajero puede vender
# sin poder anular lo ya cobrado, pero si puede procesar una devolucion en
# el mostrador (ver core/services.py _ROLE_PERMISSIONS).
_SALES_VOID_PERMISSIONS = [
    IsAuthenticated,
    TenantNotSuspended,
    TenantNotCanceled,
    RequiresFeature("HAS_SALES_MODULE"),
    HasModulePermission("SALES_VOID"),
]
_SALES_RETURN_PERMISSIONS = [
    IsAuthenticated,
    TenantNotSuspended,
    TenantNotCanceled,
    RequiresFeature("HAS_SALES_MODULE"),
    HasModulePermission("SALES_RETURN"),
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


class CustomerDebtLedgerViewSet(
    mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet
):
    """Libro de fiado (Especificacion de API §2.3, §4.5): solo lectura
    directa -el unico punto de escritura es la accion register-payment, que
    delega en CreditLedgerService.register_payment(). Las ventas a credito
    tampoco escriben aca directamente (SaleService.create_sale -> mismo
    servicio)."""

    queryset = CustomerDebtLedger.objects.all().order_by("-created_at")
    serializer_class = CustomerDebtLedgerSerializer
    permission_classes = _SALES_READ_PERMISSIONS

    def get_queryset(self):
        queryset = super().get_queryset()
        customer_id = self.request.query_params.get("customer")
        if customer_id:
            queryset = queryset.filter(customer_id=customer_id)
        return queryset

    @action(
        detail=False,
        methods=["post"],
        url_path="register-payment",
        permission_classes=_SALES_WRITE_PERMISSIONS,
    )
    def register_payment(self, request):
        serializer = RegisterDebtPaymentSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        entry = serializer.save()
        return Response(
            {
                "id": entry.id,
                "type": entry.type,
                "amount": str(entry.amount),
                "customer_current_debt": str(
                    CreditLedgerService.get_debt(entry.customer)
                ),
            },
            status=status.HTTP_201_CREATED,
        )


class CustomerBalanceLedgerViewSet(
    mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet
):
    """Libro de saldo a favor (Especificacion de API §2.3): solo lectura
    -se alimenta unicamente desde ReturnService (devolucion con
    refund_type=BALANCE) y CreditLedgerService.register_balance_use()
    (pago de una venta con saldo a favor)."""

    queryset = CustomerBalanceLedger.objects.all().order_by("-created_at")
    serializer_class = CustomerBalanceLedgerSerializer
    permission_classes = _SALES_READ_PERMISSIONS

    def get_queryset(self):
        queryset = super().get_queryset()
        customer_id = self.request.query_params.get("customer")
        if customer_id:
            queryset = queryset.filter(customer_id=customer_id)
        return queryset


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


class SaleSyncView(APIView):
    """POST -> sincroniza un lote de ventas offline (Especificacion de API
    §4.2, Sprint 20). Deliberadamente NO es una @action de SaleViewSet -es
    una URL de coleccion propia (ventas/sales/sync/), registrada antes que
    el router, mismo motivo que ventas/cash-sessions/open/ (ver comentario
    al pie de este archivo de urls.py)."""

    permission_classes = _SALES_WRITE_PERMISSIONS

    def post(self, request):
        serializer = SaleSyncSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        result = serializer.save()
        return Response(result)


class SaleViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    viewsets.GenericViewSet,
):
    """Sin update/destroy: una venta es un registro de auditoria -se corrige
    con una devolucion (SaleReturn) o una anulacion (accion void), nunca
    editando o borrando la venta original (mismo principio que CashMovement).
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
        if self.action in ("list", "retrieve", "receipt"):
            return [permission() for permission in _SALES_READ_PERMISSIONS]
        if self.action == "void":
            return [permission() for permission in _SALES_VOID_PERMISSIONS]
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

    @action(detail=True, methods=["get"], url_path="receipt")
    def receipt(self, request, pk=None):
        try:
            sale = (
                self.get_queryset().prefetch_related("details", "payments").get(pk=pk)
            )
        except Sale.DoesNotExist as exc:
            raise SaleNotFoundError() from exc

        width_mm = request.query_params.get("width_mm")
        width_mm = int(width_mm) if width_mm in ("58", "80") else 58
        html = ReceiptService.render_html(sale, request.tenant, width_mm=width_mm)
        return HttpResponse(html, content_type="text/html")

    @action(detail=True, methods=["post"], url_path="void")
    def void(self, request, pk=None):
        sale = get_object_or_404(self.get_queryset(), pk=pk)
        serializer = SaleVoidSerializer(
            data=request.data, context={"sale": sale, "request": request}
        )
        serializer.is_valid(raise_exception=True)
        sale = serializer.save()
        return Response(SaleSerializer(sale).data)


class SaleReturnViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    viewsets.GenericViewSet,
):
    """Sin update/destroy: una devolucion, igual que la venta que revierte,
    es un registro de auditoria (misma logica que SaleViewSet)."""

    queryset = SaleReturn.objects.all().order_by("-created_at")
    serializer_class = SaleReturnSerializer

    def get_serializer_class(self):
        if self.action == "create":
            return SaleReturnCreateSerializer
        return SaleReturnSerializer

    def get_permissions(self):
        if self.action in ("list", "retrieve"):
            return [permission() for permission in _SALES_READ_PERMISSIONS]
        return [permission() for permission in _SALES_RETURN_PERMISSIONS]

    def get_queryset(self):
        queryset = super().get_queryset()
        sale_id = self.request.query_params.get("sale")
        if sale_id:
            queryset = queryset.filter(sale_id=sale_id)
        return queryset

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        sale_return = serializer.save()
        return Response(
            SaleReturnSerializer(sale_return).data, status=status.HTTP_201_CREATED
        )


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


class SalesReportView(APIView):
    """GET /ventas/reports/sales/?date_from=&date_to=&warehouse=&export=
    (Sprint 24, API Spec §4.16). Ventas por periodo -una fila por venta,
    con vendedor y cliente. [ALCANCE] El desglose por producto/vendedor que
    menciona la fila original del plan queda para el Sprint 25, que ya
    reserva esa semana para terminar la cobertura de reportes exportables;
    esta version cubre el caso de uso central (exportar el periodo)."""

    permission_classes = _SALES_READ_PERMISSIONS

    def get(self, request):
        from rest_framework.exceptions import ValidationError

        from usuarios.services import ReportExportService

        date_from = request.query_params.get("date_from")
        date_to = request.query_params.get("date_to")
        if not date_from or not date_to:
            raise ValidationError("date_from y date_to son requeridos.")

        queryset = Sale.objects.select_related("customer", "user").filter(
            status="COMPLETED",
            created_at__date__gte=date_from,
            created_at__date__lte=date_to,
        )
        warehouse_id = request.query_params.get("warehouse")
        if warehouse_id:
            queryset = queryset.filter(warehouse_id=warehouse_id)

        rows = [
            {
                "invoice_number": sale.invoice_number,
                "date": sale.created_at.date().isoformat(),
                "seller": sale.user.email,
                "customer": sale.customer.name,
                "subtotal": str(sale.subtotal),
                "discount_total": str(sale.discount_total),
                "total": str(sale.total),
                "payment_status": sale.payment_status,
            }
            for sale in queryset.order_by("created_at")
        ]

        export_format = request.query_params.get("export")
        if export_format:
            columns = [
                "invoice_number",
                "date",
                "seller",
                "customer",
                "subtotal",
                "discount_total",
                "total",
                "payment_status",
            ]
            return ReportExportService.export_queryset(
                rows=rows,
                columns=columns,
                format=export_format,
                filename=f"ventas_{date_from}_a_{date_to}",
            )
        return Response(rows)


class CashSessionReportView(APIView):
    """GET /ventas/reports/cash-sessions/?date_from=&date_to=&export=
    (Sprint 25, API Spec §4.16). Sesiones de caja con su cuadre -mismo
    criterio que los demas reportes de este sprint: la misma consulta
    arma la pantalla y la exportacion."""

    permission_classes = _CASH_READ_PERMISSIONS

    def get(self, request):
        from rest_framework.exceptions import ValidationError

        from usuarios.services import ReportExportService

        date_from = request.query_params.get("date_from")
        date_to = request.query_params.get("date_to")
        if not date_from or not date_to:
            raise ValidationError("date_from y date_to son requeridos.")

        queryset = (
            CashSession.objects.select_related("cash_register", "user")
            .filter(opening_at__date__gte=date_from, opening_at__date__lte=date_to)
            .order_by("opening_at")
        )

        rows = [
            {
                "cash_register": session.cash_register.name,
                "user": session.user.email,
                "opening_at": session.opening_at.isoformat(),
                "closing_at": session.closing_at.isoformat()
                if session.closing_at
                else "",
                "opening_amount": str(session.opening_amount),
                "expected_closing_amount": str(session.expected_closing_amount)
                if session.expected_closing_amount is not None
                else "",
                "counted_closing_amount": str(session.counted_closing_amount)
                if session.counted_closing_amount is not None
                else "",
                "difference": str(session.difference)
                if session.difference is not None
                else "",
                "status": session.status,
            }
            for session in queryset
        ]

        export_format = request.query_params.get("export")
        if export_format:
            columns = [
                "cash_register",
                "user",
                "opening_at",
                "closing_at",
                "opening_amount",
                "expected_closing_amount",
                "counted_closing_amount",
                "difference",
                "status",
            ]
            return ReportExportService.export_queryset(
                rows=rows,
                columns=columns,
                format=export_format,
                filename=f"sesiones_caja_{date_from}_a_{date_to}",
            )
        return Response(rows)


class CashMovementReportView(APIView):
    """GET /ventas/reports/cash-movements/?date_from=&date_to=&export=
    (Sprint 25, API Spec §4.16). Movimientos manuales y automaticos de
    caja en un rango de fechas."""

    permission_classes = _CASH_READ_PERMISSIONS

    def get(self, request):
        from rest_framework.exceptions import ValidationError

        from usuarios.services import ReportExportService

        date_from = request.query_params.get("date_from")
        date_to = request.query_params.get("date_to")
        if not date_from or not date_to:
            raise ValidationError("date_from y date_to son requeridos.")

        queryset = (
            CashMovement.objects.select_related("cash_session__cash_register", "user")
            .filter(created_at__date__gte=date_from, created_at__date__lte=date_to)
            .order_by("created_at")
        )

        rows = [
            {
                "cash_register": movement.cash_session.cash_register.name,
                "date": movement.created_at.isoformat(),
                "type": movement.type,
                "concept": movement.concept,
                "amount": str(movement.amount),
                "reason": movement.reason,
                "user": movement.user.email,
            }
            for movement in queryset
        ]

        export_format = request.query_params.get("export")
        if export_format:
            columns = [
                "cash_register",
                "date",
                "type",
                "concept",
                "amount",
                "reason",
                "user",
            ]
            return ReportExportService.export_queryset(
                rows=rows,
                columns=columns,
                format=export_format,
                filename=f"movimientos_caja_{date_from}_a_{date_to}",
            )
        return Response(rows)
