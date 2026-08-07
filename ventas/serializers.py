from decimal import Decimal

from rest_framework import serializers

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
    SaleDetail,
    SalePayment,
    SaleReturn,
    SaleReturnDetail,
)
from ventas.services import (
    CashMovementReceiptService,
    CashSessionService,
    CreditLedgerService,
    ReturnService,
    SaleService,
)


class CashRegisterSerializer(serializers.ModelSerializer):
    class Meta:
        model = CashRegister
        fields = ["id", "warehouse", "name", "is_active"]


class CashSessionSerializer(serializers.ModelSerializer):
    class Meta:
        model = CashSession
        fields = [
            "id",
            "cash_register",
            "user",
            "opening_amount",
            "opening_at",
            "expected_closing_amount",
            "counted_closing_amount",
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
            "difference",
            "status",
            "closing_at",
        ]


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

    class Meta(CashSessionSerializer.Meta):
        fields = CashSessionSerializer.Meta.fields + ["movements"]


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

    def get_current_debt(self, obj):
        return str(CreditLedgerService.get_debt(obj))

    def get_current_balance(self, obj):
        return str(CreditLedgerService.get_balance(obj))

    def get_oldest_unpaid_debt_at(self, obj):
        if CreditLedgerService.get_debt(obj) <= 0:
            return None
        entry = obj.debt_ledger.filter(type="DEBIT").order_by("created_at").first()
        return entry.created_at if entry else None


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

    def validate_lines(self, value):
        if not value:
            raise serializers.ValidationError("Agrega al menos una linea.")
        return value

    def validate_payments(self, value):
        if not value:
            raise serializers.ValidationError("Agrega al menos un pago.")
        return value

    def create(self, validated_data):
        return SaleService.create_sale(
            customer=validated_data["customer"],
            cash_session=validated_data["cash_session"],
            user=self.context["request"].user,
            lines=validated_data["lines"],
            payments=validated_data["payments"],
            client_side_uuid=validated_data.get("client_side_uuid") or None,
        )


class SaleVoidSerializer(serializers.Serializer):
    """No es un ModelSerializer -delega toda la validacion de negocio en
    SaleService.void_sale(), mismo patron que SaleCreateSerializer."""

    reason = serializers.CharField()

    def create(self, validated_data):
        return SaleService.void_sale(
            self.context["sale"],
            reason=validated_data["reason"],
            user=self.context["request"].user,
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

    def create(self, validated_data):
        return CashSessionService.open_session(
            cash_register=validated_data["cash_register"],
            user=self.context["request"].user,
            opening_amount=validated_data["opening_amount"],
        )


class CashSessionCloseSerializer(serializers.Serializer):
    counted_closing_amount = serializers.DecimalField(
        max_digits=12, decimal_places=4, min_value=0
    )
    notes = serializers.CharField(required=False, allow_blank=True)

    def create(self, validated_data):
        return CashSessionService.close_session(
            session=self.context["session"],
            counted_closing_amount=validated_data["counted_closing_amount"],
            user=self.context["request"].user,
            tenant=self.context["request"].tenant,
            notes=validated_data.get("notes"),
        )
