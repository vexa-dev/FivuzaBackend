from decimal import Decimal

from django.db import transaction
from rest_framework import serializers

from inventario.models import (
    Attribute,
    AttributeValue,
    Brand,
    Category,
    InventoryMovement,
    Product,
    ProductTax,
    ProductVariant,
    PurchaseOrder,
    PurchaseOrderDetail,
    Stock,
    Supplier,
    TaxRate,
    VariantAttributeValue,
    VolumePricingTier,
    Warehouse,
)
from inventario.services import MediaService, ProductVariantService, StockService
from core.warehouse_access import WarehouseAccessService


class WarehouseSerializer(serializers.ModelSerializer):
    class Meta:
        model = Warehouse
        fields = ["id", "name", "address", "is_active", "created_at"]
        read_only_fields = ["created_at"]


class CategorySerializer(serializers.ModelSerializer):
    allowed_attributes = serializers.PrimaryKeyRelatedField(
        queryset=Attribute.objects.all(), many=True, required=False
    )

    class Meta:
        model = Category
        fields = [
            "id",
            "name",
            "is_active",
            "primary_attribute",
            "allowed_attributes",
        ]

    def validate(self, attrs):
        primary_attribute = attrs.get(
            "primary_attribute",
            self.instance.primary_attribute if self.instance else None,
        )
        allowed_attributes = attrs.get("allowed_attributes")
        if allowed_attributes is None:
            allowed_attributes = (
                list(self.instance.allowed_attributes.all()) if self.instance else []
            )

        if primary_attribute and primary_attribute not in allowed_attributes:
            raise serializers.ValidationError(
                {
                    "primary_attribute": (
                        "El atributo de agrupación debe estar incluido entre los "
                        "atributos permitidos."
                    )
                }
            )
        return attrs


class BrandSerializer(serializers.ModelSerializer):
    class Meta:
        model = Brand
        fields = ["id", "name", "is_active"]


class SupplierSerializer(serializers.ModelSerializer):
    class Meta:
        model = Supplier
        fields = ["id", "ruc_or_dni", "company_name", "phone", "address"]


class AttributeValueSerializer(serializers.ModelSerializer):
    class Meta:
        model = AttributeValue
        fields = ["id", "attribute", "value"]


class AttributeSerializer(serializers.ModelSerializer):
    values = AttributeValueSerializer(many=True, read_only=True)

    class Meta:
        model = Attribute
        fields = ["id", "name", "values"]


class VariantAttributeValueSerializer(serializers.ModelSerializer):
    class Meta:
        model = VariantAttributeValue
        fields = ["id", "attribute_value"]
        read_only_fields = fields


class ProductVariantSerializer(serializers.ModelSerializer):
    """attribute_value_ids es write_only: arma la combinacion de atributos
    (ej. talla=M, color=Azul) de la variante en el mismo POST -evita un
    segundo viaje al endpoint de variant-attribute-values para el caso comun."""

    attribute_values = VariantAttributeValueSerializer(many=True, read_only=True)
    attribute_value_ids = serializers.PrimaryKeyRelatedField(
        queryset=AttributeValue.objects.all(),
        many=True,
        write_only=True,
        required=False,
    )

    class Meta:
        model = ProductVariant
        fields = [
            "id",
            "product",
            "sku",
            "barcode",
            "cost",
            "price",
            "min_stock",
            "image_url",
            "is_default",
            "is_active",
            "attribute_values",
            "attribute_value_ids",
            "updated_at",
        ]
        read_only_fields = ["is_default", "updated_at"]

    def create(self, validated_data):
        attribute_value_ids = [
            value.id for value in validated_data.pop("attribute_value_ids", [])
        ]
        product = validated_data.pop("product")
        return ProductVariantService.add_variant(
            product, {**validated_data, "attribute_value_ids": attribute_value_ids}
        )


class ProductVariantImageUploadURLSerializer(serializers.Serializer):
    """Payload de entrada de POST /product-variants/{id}/upload-image-url/."""

    content_type = serializers.ChoiceField(
        choices=["image/jpeg", "image/png", "image/webp"]
    )

    def create(self, validated_data):
        return MediaService.build_variant_image_upload_url(
            self.context["variant"].id, validated_data["content_type"]
        )


class ProductVariantCreateInputSerializer(serializers.Serializer):
    """Valida cada variante incluida al crear un producto.

    Antes ``variants_input`` aceptaba diccionarios sin estructura y los
    errores terminaban apareciendo recién en la base de datos. Este
    serializer mantiene el mismo payload, pero devuelve errores 400
    legibles antes de abrir la transacción de creación.
    """

    sku = serializers.CharField(max_length=100)
    barcode = serializers.CharField(
        max_length=100, required=False, allow_blank=True, allow_null=True
    )
    cost = serializers.DecimalField(
        max_digits=12, decimal_places=4, min_value=Decimal("0"), default=Decimal("0")
    )
    price = serializers.DecimalField(
        max_digits=12, decimal_places=4, min_value=Decimal("0"), default=Decimal("0")
    )
    min_stock = serializers.DecimalField(
        max_digits=12, decimal_places=3, min_value=Decimal("0"), default=Decimal("0")
    )
    attribute_value_ids = serializers.PrimaryKeyRelatedField(
        queryset=AttributeValue.objects.all(), many=True, required=False
    )

    def validate_sku(self, value):
        if ProductVariant.all_objects.filter(sku=value).exists():
            raise serializers.ValidationError("Ya existe una variante con este SKU.")
        return value

    def validate_barcode(self, value):
        if not value:
            return None
        if ProductVariant.all_objects.filter(barcode=value).exists():
            raise serializers.ValidationError(
                "Ya existe una variante con este código de barras."
            )
        return value

    def validate_attribute_value_ids(self, values):
        attribute_ids = [value.attribute_id for value in values]
        if len(attribute_ids) != len(set(attribute_ids)):
            raise serializers.ValidationError(
                "Selecciona como máximo un valor por atributo."
            )
        return values


class ProductSerializer(serializers.ModelSerializer):
    """variants es write_only en create(): el frontend siempre manda al
    menos 1 fila (aunque el producto no tenga variantes reales), y
    ProductVariantService.create_product() garantiza la invariante
    is_default incluso si llegara vacio (Esquema Backend §5.2). Para editar
    variantes de un producto ya creado se usa el ViewSet de product-variants
    directamente, no este endpoint."""

    variants = ProductVariantSerializer(many=True, read_only=True)
    variants_input = ProductVariantCreateInputSerializer(
        many=True, write_only=True, required=True, allow_empty=False
    )

    class Meta:
        model = Product
        fields = [
            "id",
            "type",
            "name",
            "description",
            "category",
            "brand",
            "supplier",
            "unit_of_measure",
            "is_for_sale",
            "is_active",
            "variants",
            "variants_input",
            "updated_at",
            "created_at",
        ]
        read_only_fields = ["updated_at", "created_at"]

    def validate(self, attrs):
        variants = attrs.get("variants_input", [])
        skus = [variant["sku"] for variant in variants]
        if len(skus) != len(set(skus)):
            raise serializers.ValidationError(
                {"variants_input": "Los SKU de las variantes no pueden repetirse."}
            )

        barcodes = [
            variant.get("barcode") for variant in variants if variant.get("barcode")
        ]
        if len(barcodes) != len(set(barcodes)):
            raise serializers.ValidationError(
                {
                    "variants_input": (
                        "Los códigos de barras de las variantes no pueden repetirse."
                    )
                }
            )

        category = attrs.get("category")
        if category:
            allowed_ids = set(category.allowed_attributes.values_list("id", flat=True))
            for index, variant in enumerate(variants):
                selected = variant.get("attribute_value_ids", [])
                invalid = [
                    value.attribute.name
                    for value in selected
                    if value.attribute_id not in allowed_ids
                ]
                if invalid:
                    names = ", ".join(sorted(set(invalid)))
                    raise serializers.ValidationError(
                        {
                            "variants_input": (
                                f"La variante {index + 1} usa atributos no permitidos "
                                f"para la categoría: {names}."
                            )
                        }
                    )
        return attrs

    def create(self, validated_data):
        variants_data = validated_data.pop("variants_input", [])
        normalized_variants = []
        for variant_data in variants_data:
            variant_data = dict(variant_data)
            attribute_values = variant_data.pop("attribute_value_ids", [])
            normalized_variants.append(
                {
                    **variant_data,
                    "attribute_value_ids": [value.id for value in attribute_values],
                }
            )
        return ProductVariantService.create_product(
            product_data=validated_data, variants_data=normalized_variants
        )


class StockSerializer(serializers.ModelSerializer):
    class Meta:
        model = Stock
        fields = ["id", "variant", "warehouse", "quantity", "updated_at"]
        read_only_fields = fields


class InventoryMovementSerializer(serializers.ModelSerializer):
    """Solo lectura -el Kardex nunca se edita a mano, se construye
    exclusivamente via StockService (y, a partir de ventas/compras,
    tambien via esos servicios) (API Spec §4.6)."""

    class Meta:
        model = InventoryMovement
        fields = [
            "id",
            "variant",
            "warehouse",
            "user",
            "type",
            "quantity",
            "concept",
            "resulting_balance",
            "oversell_flag",
            "created_at",
        ]
        read_only_fields = fields


class LowStockVariantSerializer(serializers.ModelSerializer):
    """Salida de GET /inventario/stock/low-stock/ -incluye el nombre del
    producto y el total ya agregado (get_low_stock_variants()) para que el
    badge del layout no tenga que hacer una segunda consulta por variante."""

    product_name = serializers.CharField(source="product.name", read_only=True)
    total_stock = serializers.DecimalField(
        max_digits=12, decimal_places=3, read_only=True
    )

    class Meta:
        model = ProductVariant
        fields = ["id", "sku", "product_name", "total_stock", "min_stock"]
        read_only_fields = fields


class StockAdjustSerializer(serializers.Serializer):
    """Payload de entrada de POST /inventario/stock/adjust/: cubre tanto
    conteo fisico como merma, ambos son "la cantidad real es esta" desde
    el punto de vista de StockService -no un delta que el frontend calcula
    a mano (API Spec §4.6)."""

    variant = serializers.PrimaryKeyRelatedField(queryset=ProductVariant.objects.all())
    warehouse = serializers.PrimaryKeyRelatedField(queryset=Warehouse.objects.all())
    counted_quantity = serializers.DecimalField(max_digits=12, decimal_places=3)
    concept = serializers.ChoiceField(
        choices=["PURCHASE", "SALE", "ADJUSTMENT", "RETURN"], default="ADJUSTMENT"
    )

    def validate_warehouse(self, warehouse):
        WarehouseAccessService.require_warehouse(
            self.context["request"].user, warehouse
        )
        return warehouse

    def create(self, validated_data):
        return StockService.adjust_stock(
            variant=validated_data["variant"],
            warehouse=validated_data["warehouse"],
            counted_quantity=validated_data["counted_quantity"],
            concept=validated_data["concept"],
            user=self.context["request"].user,
        )


class StockTransferSerializer(serializers.Serializer):
    """Payload de entrada de POST /inventario/stock/transfer/ (Sprint 26,
    API Spec §2.2)."""

    variant = serializers.PrimaryKeyRelatedField(queryset=ProductVariant.objects.all())
    from_warehouse = serializers.PrimaryKeyRelatedField(
        queryset=Warehouse.objects.all()
    )
    to_warehouse = serializers.PrimaryKeyRelatedField(queryset=Warehouse.objects.all())
    quantity = serializers.DecimalField(max_digits=12, decimal_places=3)

    def validate(self, attrs):
        user = self.context["request"].user
        WarehouseAccessService.require_warehouse(user, attrs["from_warehouse"])
        WarehouseAccessService.require_warehouse(user, attrs["to_warehouse"])
        return attrs

    def create(self, validated_data):
        out_movement, in_movement = StockService.transfer_stock(
            variant=validated_data["variant"],
            from_warehouse=validated_data["from_warehouse"],
            to_warehouse=validated_data["to_warehouse"],
            quantity=validated_data["quantity"],
            user=self.context["request"].user,
        )
        return {"out_movement": out_movement, "in_movement": in_movement}


class VolumePricingTierSerializer(serializers.ModelSerializer):
    class Meta:
        model = VolumePricingTier
        fields = ["id", "variant", "min_quantity", "unit_price"]


class LabelItemSerializer(serializers.Serializer):
    """Una entrada de la hoja de etiquetas: variante + cuántas copias."""

    variant_id = serializers.PrimaryKeyRelatedField(
        queryset=ProductVariant.objects.all(), source="variant"
    )
    quantity = serializers.IntegerField(min_value=1, max_value=200)


class PrintLabelsSerializer(serializers.Serializer):
    """Payload de entrada de POST /inventario/labels/print/ (Sprint 27,
    API Spec §2.2)."""

    items = LabelItemSerializer(many=True)
    size = serializers.ChoiceField(choices=["40x25", "50x30"], default="40x25")


class TaxRateSerializer(serializers.ModelSerializer):
    class Meta:
        model = TaxRate
        fields = ["id", "name", "percentage", "is_active"]


class ProductTaxSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProductTax
        fields = ["id", "variant", "tax_rate"]


class PurchaseOrderDetailSerializer(serializers.ModelSerializer):
    class Meta:
        model = PurchaseOrderDetail
        fields = [
            "id",
            "purchase_order",
            "variant_id",
            "quantity",
            "unit_cost",
            "subtotal",
        ]
        read_only_fields = ["subtotal"]


class PurchaseOrderSerializer(serializers.ModelSerializer):
    """details es de solo lectura -las lineas se arman en create() a
    partir de details_input, calculando subtotal/total en el servidor
    (nunca confiando en el total que mande el cliente)."""

    details = PurchaseOrderDetailSerializer(many=True, read_only=True)
    details_input = serializers.ListField(
        child=serializers.DictField(), write_only=True
    )

    class Meta:
        model = PurchaseOrder
        fields = [
            "id",
            "supplier",
            "warehouse",
            "user",
            "invoice_number",
            "status",
            "total",
            "currency",
            "received_at",
            "details",
            "details_input",
            "created_at",
        ]
        read_only_fields = ["user", "status", "total", "received_at", "created_at"]

    def validate_warehouse(self, warehouse):
        WarehouseAccessService.require_warehouse(
            self.context["request"].user, warehouse
        )
        return warehouse

    def create(self, validated_data):
        details_data = validated_data.pop("details_input", [])
        if not details_data:
            raise serializers.ValidationError(
                {"details_input": "Se requiere al menos una linea."}
            )

        request = self.context["request"]
        with transaction.atomic():
            order = PurchaseOrder.objects.create(
                user=request.user, total=Decimal("0"), **validated_data
            )
            total = Decimal("0")
            for line in details_data:
                quantity = Decimal(str(line["quantity"]))
                unit_cost = Decimal(str(line["unit_cost"]))
                subtotal = quantity * unit_cost
                total += subtotal
                PurchaseOrderDetail.objects.create(
                    purchase_order=order,
                    variant_id=line["variant_id"],
                    quantity=quantity,
                    unit_cost=unit_cost,
                    subtotal=subtotal,
                )
            order.total = total
            order.save(update_fields=["total"])
        return order
