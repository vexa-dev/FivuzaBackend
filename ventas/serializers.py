from rest_framework import serializers

from ventas.models import (
    CashMovement,
    CashRegister,
    CashSession,
    Customer,
    Promotion,
    PromotionProduct,
)
from ventas.services import CashMovementReceiptService, CashSessionService


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
            "updated_at",
            "created_at",
        ]
        read_only_fields = ["updated_at", "created_at"]


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
