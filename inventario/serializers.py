from rest_framework import serializers

from inventario.models import (
    Attribute,
    AttributeValue,
    Category,
    Product,
    ProductVariant,
    Supplier,
    VariantAttributeValue,
    Warehouse,
)
from inventario.services import MediaService, ProductVariantService


class WarehouseSerializer(serializers.ModelSerializer):
    class Meta:
        model = Warehouse
        fields = ["id", "name", "address", "is_active", "created_at"]
        read_only_fields = ["created_at"]


class CategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = Category
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


class ProductSerializer(serializers.ModelSerializer):
    """variants es write_only en create(): el frontend siempre manda al
    menos 1 fila (aunque el producto no tenga variantes reales), y
    ProductVariantService.create_product() garantiza la invariante
    is_default incluso si llegara vacio (Esquema Backend §5.2). Para editar
    variantes de un producto ya creado se usa el ViewSet de product-variants
    directamente, no este endpoint."""

    variants = ProductVariantSerializer(many=True, read_only=True)
    variants_input = serializers.ListField(
        child=serializers.DictField(), write_only=True, required=False
    )

    class Meta:
        model = Product
        fields = [
            "id",
            "type",
            "name",
            "description",
            "category",
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

    def create(self, validated_data):
        variants_data = validated_data.pop("variants_input", [])
        return ProductVariantService.create_product(
            product_data=validated_data, variants_data=variants_data
        )
