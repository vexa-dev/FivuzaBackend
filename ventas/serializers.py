from decimal import Decimal

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from inventario.models import ProductVariant, Warehouse
from core.warehouse_access import WarehouseAccessService
from usuarios.authorization import authorization_token_from
from ventas.models import (
    CashMovement,
    CashRegister,
    CashSession,
    Customer,
    CustomerBalanceLedger,
    CustomerDebtLedger,
    ProductReservation,
    Promotion,
    PromotionProduct,
    Quote,
    QuoteDetail,
    Sale,
    SaleDetail,
    SalePayment,
    SaleReturn,
    SaleReturnDetail,
)
from ventas.services import (
    CashMovementReceiptService,
    CashSessionService,
    CreditLedgerService,
    QuoteService,
    ReservationService,
    ReturnService,
    SaleService,
    SaleSyncService,
)


class CashRegisterSerializer(serializers.ModelSerializer):
    class Meta:
        model = CashRegister
        fields = ["id", "warehouse", "name", "is_active", "assigned_user"]

    def validate_warehouse(self, warehouse):
        WarehouseAccessService.require_warehouse(
            self.context["request"].user, warehouse
        )
        return warehouse

    def validate(self, attrs):
        """Bloque A.2: asignar la caja a alguien que no trabaja en ese
        almacen, o a alguien dado de baja, deja una caja que nadie puede
        abrir -es un error de configuracion silencioso, asi que se rechaza
        al guardarlo y no al intentar vender."""
        assigned_user = attrs.get(
            "assigned_user", getattr(self.instance, "assigned_user", None)
        )
        if assigned_user is None:
            return attrs

        if not assigned_user.is_active or assigned_user.deleted_at is not None:
            raise serializers.ValidationError(
                {"assigned_user": "Esta persona ya no está activa en el sistema."}
            )

        warehouse = attrs.get("warehouse", getattr(self.instance, "warehouse", None))
        allowed = WarehouseAccessService.allowed_warehouse_ids(assigned_user)
        if warehouse is not None and warehouse.id not in allowed:
            raise serializers.ValidationError(
                {
                    "assigned_user": (
                        "Esta persona no tiene acceso al almacén de esta caja."
                    )
                }
            )
        return attrs


class CashSessionSerializer(serializers.ModelSerializer):
    # Esperado "a la fecha" de una caja abierta, con la misma formula del
    # cierre real (apertura + ventas en efectivo + ingresos - egresos).
    #
    # Bloque A.3 (arqueo a ciegas): solo lo recibe quien tiene CASH_CLOSE
    # por permiso propio. Para el cajero -incluido aquel a quien el dueño le
    # concedio el cierre con el interruptor- viaja en null: contar el
    # efectivo sabiendo cuanto "deberia" haber no es un control, es un
    # formulario con la respuesta impresa al lado. El backend no envia el
    # dato; no se confia en que la UI lo esconda.
    expected_amount_so_far = serializers.SerializerMethodField()

    class Meta:
        model = CashSession
        fields = [
            "id",
            "cash_register",
            "user",
            "opening_amount",
            "opening_at",
            "expected_closing_amount",
            "expected_amount_so_far",
            "counted_closing_amount",
            "counted_at",
            "approved_by",
            "difference",
            "status",
            "closing_at",
            "notes",
        ]
        read_only_fields = [
            "user",
            "opening_at",
            "expected_closing_amount",
            "counted_closing_amount",
            "counted_at",
            "approved_by",
            "difference",
            "status",
            "closing_at",
        ]

    # Campos del arqueo que solo ve quien controla la caja (Bloque A.3).
    _CONTROL_FIELDS = (
        "expected_amount_so_far",
        "expected_closing_amount",
        "difference",
    )

    def to_representation(self, session):
        data = super().to_representation(session)
        if not self._viewer_closes_cash():
            for field in self._CONTROL_FIELDS:
                if field in data:
                    data[field] = None
        return data

    @extend_schema_field(
        serializers.DecimalField(max_digits=14, decimal_places=4, allow_null=True)
    )
    def get_expected_amount_so_far(self, session):
        if session.status not in ("OPEN", "PENDING_APPROVAL"):
            return None
        expected = CashSessionService._calculate_expected_closing_amount(session)
        return str(expected)

    def _viewer_closes_cash(self) -> bool:
        """Quien controla la caja, es decir quien tiene CASH_CLOSE. El
        cajero al que el negocio le permitio entregar su caja tiene
        CASH_SUBMIT_COUNT, que no alcanza (Bloque A.3)."""
        from usuarios.services import PermissionService

        request = self.context.get("request")
        user = getattr(request, "user", None)
        if user is None or not hasattr(user, "role_id"):
            return False
        return PermissionService.check_permission(user, "CASH_CLOSE")


class CashMovementSerializer(serializers.ModelSerializer):
    """create() delega en CashSessionService.add_movement() en vez de
    insertar directamente -es el unico punto que valida que la sesion siga
    OPEN antes de aceptar un movimiento (Sprint 12, Infra/QA)."""

    class Meta:
        model = CashMovement
        fields = [
            "id",
            "cash_session",
            "type",
            "concept",
            "amount",
            "reason",
            "receipt_url",
            "user",
            "created_at",
        ]
        read_only_fields = ["user", "created_at"]

    def validate_cash_session(self, session):
        WarehouseAccessService.require_warehouse(
            self.context["request"].user, session.cash_register.warehouse_id
        )
        return session

    def create(self, validated_data):
        return CashSessionService.add_movement(
            session=validated_data["cash_session"],
            type=validated_data["type"],
            concept=validated_data["concept"],
            amount=validated_data["amount"],
            user=self.context["request"].user,
            reason=validated_data.get("reason", ""),
            receipt_url=validated_data.get("receipt_url"),
        )


class CashSessionDetailSerializer(CashSessionSerializer):
    """Retrieve de una sesion incluye sus movimientos anidados -"detalle de
    una sesion cerrada con todos sus movimientos" (Especificacion de API
    §2.3), sin requerir una segunda llamada a /cash-movements/?cash_session=."""

    movements = CashMovementSerializer(many=True, read_only=True)
    # Bloque A.4: totales por metodo de pago del turno. A diferencia del
    # esperado del arqueo, esto SI lo ve el cajero -no le dice cuanto
    # efectivo deberia tener en el cajon, le dice que cobro por cada medio.
    payment_totals = serializers.SerializerMethodField()

    class Meta(CashSessionSerializer.Meta):
        fields = CashSessionSerializer.Meta.fields + ["movements", "payment_totals"]

    @extend_schema_field(
        serializers.DictField(child=serializers.CharField(), allow_null=False)
    )
    def get_payment_totals(self, session):
        return CashSessionService.payment_totals_by_method(session)


class CashMovementReceiptUploadURLSerializer(serializers.Serializer):
    """No es un ModelSerializer: un CashMovement todavia no existe cuando se
    pide la URL prefirmada -mismo motivo por el que la key de S3 no depende
    de un pk existente (ver ventas.services.CashMovementReceiptService)."""

    content_type = serializers.CharField()

    def create(self, validated_data):
        try:
            return CashMovementReceiptService.build_receipt_upload_url(
                validated_data["content_type"]
            )
        except ValueError as exc:
            raise serializers.ValidationError({"content_type": str(exc)}) from exc


class CustomerSerializer(serializers.ModelSerializer):
    """current_debt/current_balance/oldest_unpaid_debt_at (Sprint 19) se
    calculan en cada serializacion -sin annotate/optimizar, mismo criterio
    que el resto del proyecto para catalogos de tamaño de negocio pequeño
    (Convenciones: no optimizar para una escala que todavia no existe)."""

    current_debt = serializers.SerializerMethodField()
    current_balance = serializers.SerializerMethodField()
    oldest_unpaid_debt_at = serializers.SerializerMethodField()

    class Meta:
        model = Customer
        fields = [
            "id",
            "document_type",
            "document_number",
            "name",
            "phone",
            "address",
            "is_active",
            "credit_limit",
            "current_debt",
            "current_balance",
            "oldest_unpaid_debt_at",
            "updated_at",
            "created_at",
        ]
        read_only_fields = ["updated_at", "created_at"]

    def get_current_debt(self, obj) -> str:
        return str(CreditLedgerService.get_debt(obj))

    def get_current_balance(self, obj) -> str:
        return str(CreditLedgerService.get_balance(obj))

    def get_oldest_unpaid_debt_at(self, obj) -> str | None:
        if CreditLedgerService.get_debt(obj) <= 0:
            return None
        entry = obj.debt_ledger.filter(type="DEBIT").order_by("created_at").first()
        return entry.created_at.isoformat() if entry else None


class CustomerDebtLedgerSerializer(serializers.ModelSerializer):
    class Meta:
        model = CustomerDebtLedger
        fields = [
            "id",
            "customer",
            "sale",
            "type",
            "amount",
            "currency",
            "description",
            "created_at",
        ]
        read_only_fields = fields


class CustomerBalanceLedgerSerializer(serializers.ModelSerializer):
    class Meta:
        model = CustomerBalanceLedger
        fields = [
            "id",
            "customer",
            "sale",
            "sale_return",
            "type",
            "amount",
            "currency",
            "description",
            "created_at",
        ]
        read_only_fields = fields


class RegisterDebtPaymentSerializer(serializers.Serializer):
    """No es un ModelSerializer -delega en CreditLedgerService.
    register_payment(), mismo patron que el resto de acciones de negocio
    (SaleCreateSerializer, CashSessionOpenSerializer)."""

    customer_id = serializers.PrimaryKeyRelatedField(
        source="customer", queryset=Customer.objects.all()
    )
    amount = serializers.DecimalField(
        max_digits=12, decimal_places=4, min_value=Decimal("0.01")
    )
    description = serializers.CharField(required=False, allow_blank=True)

    def create(self, validated_data):
        return CreditLedgerService.register_payment(
            customer=validated_data["customer"],
            amount=validated_data["amount"],
            description=validated_data.get("description", ""),
        )


class PromotionProductSerializer(serializers.ModelSerializer):
    class Meta:
        model = PromotionProduct
        fields = ["id", "promotion", "variant", "category"]

    def validate(self, attrs):
        variant = attrs.get("variant")
        category = attrs.get("category")
        if bool(variant) == bool(category):
            raise serializers.ValidationError(
                "Especifique variant o category, nunca ambos ni ninguno."
            )
        return attrs


class PromotionSerializer(serializers.ModelSerializer):
    targets = PromotionProductSerializer(many=True, read_only=True)

    class Meta:
        model = Promotion
        fields = [
            "id",
            "name",
            "type",
            "value",
            "start_date",
            "end_date",
            "is_active",
            "targets",
            "updated_at",
        ]
        read_only_fields = ["updated_at"]


class SaleDetailSerializer(serializers.ModelSerializer):
    class Meta:
        model = SaleDetail
        fields = [
            "id",
            "variant_id",
            "product_name_snapshot",
            "sku_snapshot",
            "quantity",
            "unit_price",
            "discount_amount",
            "subtotal",
        ]


class SalePaymentSerializer(serializers.ModelSerializer):
    class Meta:
        model = SalePayment
        fields = ["id", "method", "amount", "created_at"]


class SaleSerializer(serializers.ModelSerializer):
    details = SaleDetailSerializer(many=True, read_only=True)
    payments = SalePaymentSerializer(many=True, read_only=True)

    class Meta:
        model = Sale
        fields = [
            "id",
            "invoice_number",
            "customer",
            "user",
            "warehouse",
            "cash_session",
            "subtotal",
            "discount_total",
            "total",
            "currency",
            "payment_status",
            "status",
            "sync_status",
            "details",
            "payments",
            "created_at",
            "occurred_at",
        ]


class SaleLineInputSerializer(serializers.Serializer):
    variant_id = serializers.IntegerField()
    quantity = serializers.DecimalField(
        max_digits=12, decimal_places=3, min_value=Decimal("0.001")
    )
    discount_amount = serializers.DecimalField(
        max_digits=12, decimal_places=4, min_value=0, required=False, allow_null=True
    )


class SalePaymentInputSerializer(serializers.Serializer):
    method = serializers.ChoiceField(
        choices=["CASH", "CARD", "YAPE", "CREDIT_LEDGER", "BALANCE"]
    )
    amount = serializers.DecimalField(
        max_digits=12, decimal_places=4, min_value=Decimal("0.01")
    )


class SaleCreateSerializer(serializers.Serializer):
    """No es un ModelSerializer -delega toda la validacion de negocio
    (stock, pagos, sesion de caja) en SaleService.create_sale(), mismo
    patron que CashSessionOpenSerializer/CloseSerializer (Sprint 12)."""

    customer_id = serializers.PrimaryKeyRelatedField(
        source="customer", queryset=Customer.objects.all()
    )
    cash_session_id = serializers.PrimaryKeyRelatedField(
        source="cash_session", queryset=CashSession.objects.all()
    )
    client_side_uuid = serializers.CharField(required=False, allow_blank=True)
    lines = SaleLineInputSerializer(many=True)
    payments = SalePaymentInputSerializer(many=True)

    def validate_cash_session_id(self, session):
        WarehouseAccessService.require_warehouse(
            self.context["request"].user, session.cash_register.warehouse_id
        )
        return session

    def validate_lines(self, value):
        if not value:
            raise serializers.ValidationError("Agrega al menos una linea.")
        return value

    def validate_payments(self, value):
        if not value:
            raise serializers.ValidationError("Agrega al menos un pago.")
        return value

    def create(self, validated_data):
        request = self.context["request"]
        return SaleService.create_sale(
            customer=validated_data["customer"],
            cash_session=validated_data["cash_session"],
            user=request.user,
            lines=validated_data["lines"],
            payments=validated_data["payments"],
            client_side_uuid=validated_data.get("client_side_uuid") or None,
            authorization_token=authorization_token_from(request),
        )


class SaleSyncItemSerializer(serializers.Serializer):
    """Una venta dentro del lote de /ventas/sales/sync/ (Sprint 20). A
    diferencia de SaleCreateSerializer, client_side_uuid es obligatorio -es
    la clave de deduplicacion, sin el no hay forma de saber si esta venta
    ya se sincronizo en un intento anterior.

    customer_id/cash_session_id son enteros simples, no
    PrimaryKeyRelatedField: si uno no existe (o el usuario no tiene acceso
    al almacen de la caja), solo esa venta debe quedar FAILED -validarlo
    aqui rechazaba el lote completo con 400 y trababa la cola offline. La
    resolucion vive en SaleSyncService.sync_batch()."""

    client_side_uuid = serializers.CharField()
    customer_id = serializers.IntegerField()
    cash_session_id = serializers.IntegerField()
    # Hora del dispositivo POS al momento de vender (opcional para clientes
    # anteriores a este campo: si falta, se usa la hora de sincronizacion).
    occurred_at = serializers.DateTimeField(required=False)
    lines = SaleLineInputSerializer(many=True)
    payments = SalePaymentInputSerializer(many=True)

    def validate_lines(self, value):
        if not value:
            raise serializers.ValidationError("Agrega al menos una linea.")
        return value

    def validate_payments(self, value):
        if not value:
            raise serializers.ValidationError("Agrega al menos un pago.")
        return value


class SaleSyncSerializer(serializers.Serializer):
    """No es un ModelSerializer -delega toda la validacion de negocio en
    SaleSyncService.sync_batch(). DRF solo valida la forma (client_side_uuid
    presente, lines/payments no vacios, tamano del lote); la existencia de
    cliente/caja y el acceso al almacen se resuelven por venta en el
    servicio."""

    MAX_BATCH_SIZE = 500

    sales = SaleSyncItemSerializer(many=True)

    def validate_sales(self, value):
        if not value:
            raise serializers.ValidationError("El lote no puede venir vacio.")
        if len(value) > self.MAX_BATCH_SIZE:
            raise serializers.ValidationError(
                f"El lote admite como maximo {self.MAX_BATCH_SIZE} ventas."
            )
        return value

    def create(self, validated_data):
        return SaleSyncService.sync_batch(
            sales=validated_data["sales"], user=self.context["request"].user
        )


class SaleVoidSerializer(serializers.Serializer):
    """No es un ModelSerializer -delega toda la validacion de negocio en
    SaleService.void_sale(), mismo patron que SaleCreateSerializer."""

    reason = serializers.CharField()

    def create(self, validated_data):
        request = self.context["request"]
        return SaleService.void_sale(
            self.context["sale"],
            reason=validated_data["reason"],
            user=request.user,
            authorization_token=authorization_token_from(request),
        )


class SaleReturnDetailSerializer(serializers.ModelSerializer):
    class Meta:
        model = SaleReturnDetail
        fields = ["id", "sale_detail", "quantity_returned", "restock", "subtotal"]


class SaleReturnSerializer(serializers.ModelSerializer):
    details = SaleReturnDetailSerializer(many=True, read_only=True)

    class Meta:
        model = SaleReturn
        fields = [
            "id",
            "sale",
            "user",
            "reason",
            "total_refund_amount",
            "refund_type",
            "details",
            "created_at",
        ]


class SaleReturnItemInputSerializer(serializers.Serializer):
    sale_detail_id = serializers.IntegerField()
    quantity_returned = serializers.DecimalField(
        max_digits=12, decimal_places=3, min_value=Decimal("0.001")
    )
    restock = serializers.BooleanField(required=False, default=True)


class SaleReturnCreateSerializer(serializers.Serializer):
    """No es un ModelSerializer -delega toda la validacion de negocio
    (cantidad devuelta vs. vendida, reingreso de stock, reembolso) en
    ReturnService.create_return(), mismo patron que SaleCreateSerializer."""

    sale_id = serializers.PrimaryKeyRelatedField(
        source="sale", queryset=Sale.objects.all()
    )
    reason = serializers.CharField(required=False, allow_blank=True)
    refund_type = serializers.ChoiceField(choices=["BALANCE", "CASH"])
    cash_session_id = serializers.PrimaryKeyRelatedField(
        source="cash_session",
        queryset=CashSession.objects.all(),
        required=False,
        allow_null=True,
    )
    items = SaleReturnItemInputSerializer(many=True)

    def validate_sale_id(self, sale):
        WarehouseAccessService.require_warehouse(
            self.context["request"].user, sale.warehouse_id
        )
        return sale

    def validate_cash_session_id(self, session):
        if session is not None:
            WarehouseAccessService.require_warehouse(
                self.context["request"].user, session.cash_register.warehouse_id
            )
        return session

    def validate_items(self, value):
        if not value:
            raise serializers.ValidationError("Agrega al menos una linea a devolver.")
        return value

    def create(self, validated_data):
        return ReturnService.create_return(
            sale=validated_data["sale"],
            items=validated_data["items"],
            reason=validated_data.get("reason", ""),
            refund_type=validated_data["refund_type"],
            user=self.context["request"].user,
            cash_session=validated_data.get("cash_session"),
            authorization_token=authorization_token_from(self.context["request"]),
        )


class CashSessionOpenSerializer(serializers.Serializer):
    """No es un ModelSerializer -delega la validacion de "una sola sesion
    abierta por caja" a CashSessionService.open_session(), igual que
    StockAdjustSerializer delega en StockService (Sprint 5)."""

    cash_register_id = serializers.PrimaryKeyRelatedField(
        source="cash_register", queryset=CashRegister.objects.filter(is_active=True)
    )
    opening_amount = serializers.DecimalField(
        max_digits=12, decimal_places=4, min_value=0
    )

    def validate_cash_register_id(self, cash_register):
        WarehouseAccessService.require_warehouse(
            self.context["request"].user, cash_register.warehouse_id
        )
        return cash_register

    def create(self, validated_data):
        return CashSessionService.open_session(
            cash_register=validated_data["cash_register"],
            user=self.context["request"].user,
            opening_amount=validated_data["opening_amount"],
        )


class CashSessionSubmitCountSerializer(serializers.Serializer):
    """Primer paso del cierre (Bloque A): el cajero entrega lo que conto."""

    counted_closing_amount = serializers.DecimalField(
        max_digits=12, decimal_places=4, min_value=0
    )
    notes = serializers.CharField(required=False, allow_blank=True)

    def validate(self, attrs):
        WarehouseAccessService.require_warehouse(
            self.context["request"].user,
            self.context["session"].cash_register.warehouse_id,
        )
        return attrs

    def create(self, validated_data):
        return CashSessionService.submit_count(
            session=self.context["session"],
            counted_closing_amount=validated_data["counted_closing_amount"],
            user=self.context["request"].user,
            notes=validated_data.get("notes"),
        )


class CashSessionCloseSerializer(serializers.Serializer):
    # Opcional desde el cierre en dos pasos: si el cajero ya entrego su
    # conteo, el supervisor confirma sin volver a escribirlo.
    counted_closing_amount = serializers.DecimalField(
        max_digits=12, decimal_places=4, min_value=0, required=False
    )
    notes = serializers.CharField(required=False, allow_blank=True)

    def validate(self, attrs):
        WarehouseAccessService.require_warehouse(
            self.context["request"].user,
            self.context["session"].cash_register.warehouse_id,
        )
        return attrs

    def create(self, validated_data):
        return CashSessionService.close_session(
            session=self.context["session"],
            counted_closing_amount=validated_data.get("counted_closing_amount"),
            user=self.context["request"].user,
            tenant=self.context["request"].tenant,
            notes=validated_data.get("notes"),
        )


class ProductReservationSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProductReservation
        fields = [
            "id",
            "customer",
            "variant",
            "warehouse",
            "quantity",
            "expires_at",
            "status",
            "sale",
            "user",
            "created_at",
        ]
        read_only_fields = ["status", "sale", "user", "created_at"]


class ReservationCreateSerializer(serializers.Serializer):
    """No es un ModelSerializer -delega en ReservationService.
    create_reservation(), que valida el stock disponible restando las
    reservas ACTIVE ya existentes (mismo patron que SaleCreateSerializer)."""

    customer_id = serializers.PrimaryKeyRelatedField(
        source="customer", queryset=Customer.objects.all()
    )
    variant_id = serializers.PrimaryKeyRelatedField(
        source="variant", queryset=ProductVariant.objects.all()
    )
    warehouse_id = serializers.PrimaryKeyRelatedField(
        source="warehouse", queryset=Warehouse.objects.all()
    )
    quantity = serializers.DecimalField(
        max_digits=12, decimal_places=3, min_value=Decimal("0.001")
    )
    expires_at = serializers.DateTimeField()

    def validate_warehouse_id(self, warehouse):
        WarehouseAccessService.require_warehouse(
            self.context["request"].user, warehouse
        )
        return warehouse

    def create(self, validated_data):
        return ReservationService.create_reservation(
            customer=validated_data["customer"],
            variant=validated_data["variant"],
            warehouse=validated_data["warehouse"],
            quantity=validated_data["quantity"],
            expires_at=validated_data["expires_at"],
            user=self.context["request"].user,
        )


class ReservationConvertSerializer(serializers.Serializer):
    """No es un ModelSerializer -delega en ReservationService.
    convert_to_sale(), que reutiliza SaleService.create_sale() (mismo
    patron que SaleReturnCreateSerializer)."""

    cash_session_id = serializers.PrimaryKeyRelatedField(
        source="cash_session", queryset=CashSession.objects.all()
    )
    payments = SalePaymentInputSerializer(many=True)

    def validate_cash_session_id(self, session):
        WarehouseAccessService.require_warehouse(
            self.context["request"].user, session.cash_register.warehouse_id
        )
        return session

    def validate_payments(self, value):
        if not value:
            raise serializers.ValidationError("Agrega al menos un pago.")
        return value

    def create(self, validated_data):
        return ReservationService.convert_to_sale(
            reservation=self.context["reservation"],
            cash_session=validated_data["cash_session"],
            user=self.context["request"].user,
            payments=validated_data["payments"],
        )


class QuoteDetailSerializer(serializers.ModelSerializer):
    class Meta:
        model = QuoteDetail
        fields = [
            "id",
            "variant_id",
            "product_name_snapshot",
            "sku_snapshot",
            "quantity",
            "unit_price",
            "discount_amount",
            "subtotal",
        ]


class QuoteSerializer(serializers.ModelSerializer):
    details = QuoteDetailSerializer(many=True, read_only=True)

    class Meta:
        model = Quote
        fields = [
            "id",
            "customer",
            "user",
            "status",
            "valid_until",
            "subtotal",
            "discount_total",
            "total",
            "sale",
            "details",
            "created_at",
        ]
        read_only_fields = [
            "user",
            "status",
            "subtotal",
            "discount_total",
            "total",
            "sale",
            "created_at",
        ]


class QuoteLineInputSerializer(serializers.Serializer):
    variant_id = serializers.IntegerField()
    quantity = serializers.DecimalField(
        max_digits=12, decimal_places=3, min_value=Decimal("0.001")
    )
    discount_amount = serializers.DecimalField(
        max_digits=12, decimal_places=4, min_value=0, required=False, allow_null=True
    )


class QuoteCreateSerializer(serializers.Serializer):
    """No es un ModelSerializer -delega en QuoteService.create_quote(), que
    congela precio/descuento por linea al momento de cotizar (mismo patron
    que SaleCreateSerializer, pero sin tocar stock ni caja)."""

    customer_id = serializers.PrimaryKeyRelatedField(
        source="customer", queryset=Customer.objects.all()
    )
    valid_until = serializers.DateTimeField()
    lines = QuoteLineInputSerializer(many=True)

    def validate_lines(self, value):
        if not value:
            raise serializers.ValidationError("Agrega al menos una linea.")
        return value

    def create(self, validated_data):
        request = self.context["request"]
        return QuoteService.create_quote(
            customer=validated_data["customer"],
            user=request.user,
            lines=validated_data["lines"],
            valid_until=validated_data["valid_until"],
            authorization_token=authorization_token_from(request),
        )


class QuoteConvertSerializer(serializers.Serializer):
    """No es un ModelSerializer -delega en QuoteService.convert_to_sale(),
    que pasa los precios ya congelados en QuoteDetail tal cual a
    SaleService.create_sale() (mismo patron que ReservationConvertSerializer)."""

    cash_session_id = serializers.PrimaryKeyRelatedField(
        source="cash_session", queryset=CashSession.objects.all()
    )
    payments = SalePaymentInputSerializer(many=True)

    def validate_cash_session_id(self, session):
        WarehouseAccessService.require_warehouse(
            self.context["request"].user, session.cash_register.warehouse_id
        )
        return session

    def validate_payments(self, value):
        if not value:
            raise serializers.ValidationError("Agrega al menos un pago.")
        return value

    def create(self, validated_data):
        return QuoteService.convert_to_sale(
            quote=self.context["quote"],
            cash_session=validated_data["cash_session"],
            user=self.context["request"].user,
            payments=validated_data["payments"],
        )
