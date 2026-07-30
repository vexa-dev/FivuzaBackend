import uuid
from decimal import Decimal

import boto3
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction

from inventario.models import (
    InventoryMovement,
    Product,
    ProductVariant,
    Stock,
    VariantAttributeValue,
    Warehouse,
)

_PRESIGNED_URL_TTL_SECONDS = 300
_ALLOWED_IMAGE_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp"}
_MAX_IMAGE_SIZE_BYTES = 5 * 1024 * 1024


class MediaService:
    """Genera URLs prefirmadas de S3 para que el frontend suba la imagen de
    una variante directamente al bucket, sin que el archivo pase por el
    backend (TRD §5.1) -Django solo valida tipo/tamaño y firma la URL."""

    @staticmethod
    def build_variant_image_upload_url(variant_id: int, content_type: str) -> dict:
        if content_type not in _ALLOWED_IMAGE_CONTENT_TYPES:
            raise ValueError(f"Tipo de archivo no permitido: {content_type}")

        extension = content_type.split("/")[-1]
        key = f"product-variants/{variant_id}/{uuid.uuid4()}.{extension}"

        client = boto3.client("s3", region_name=settings.AWS_S3_REGION)
        upload_url = client.generate_presigned_url(
            "put_object",
            Params={
                "Bucket": settings.AWS_STORAGE_BUCKET_NAME,
                "Key": key,
                "ContentType": content_type,
            },
            ExpiresIn=_PRESIGNED_URL_TTL_SECONDS,
        )
        image_url = (
            f"https://{settings.AWS_STORAGE_BUCKET_NAME}.s3."
            f"{settings.AWS_S3_REGION}.amazonaws.com/{key}"
        )
        return {"upload_url": upload_url, "image_url": image_url}


class ProductVariantService:
    """Único punto de entrada para crear el árbol producto + variantes.
    Garantiza que todo producto quede con al menos 1 variante marcada
    is_default=True, incluso los que no tienen variantes reales (talla,
    color) -a partir del Sprint 4, Stock e InventoryMovement siempre
    referencian una variante, nunca un producto directamente (Esquema
    Backend §5.2). Dejar esta invariante en manos de cada vista sería
    repetirla en cada punto de creación."""

    @staticmethod
    @transaction.atomic
    def create_product(*, product_data: dict, variants_data: list[dict]) -> Product:
        product = Product.objects.create(**product_data)
        ProductVariantService._create_variants(product, variants_data)
        return product

    @staticmethod
    def _create_variants(
        product: Product, variants_data: list[dict]
    ) -> list[ProductVariant]:
        if not variants_data:
            variants_data = [{"sku": f"{product.id}-DEFAULT"}]

        has_explicit_default = any(data.get("is_default") for data in variants_data)

        variants = []
        for index, data in enumerate(variants_data):
            data = dict(data)
            attribute_value_ids = data.pop("attribute_value_ids", [])
            is_default = data.pop("is_default", False) or (
                not has_explicit_default and index == 0
            )
            variant = ProductVariant.objects.create(
                product=product, is_default=is_default, **data
            )
            for attribute_value_id in attribute_value_ids:
                VariantAttributeValue.objects.create(
                    variant=variant, attribute_value_id=attribute_value_id
                )
            variants.append(variant)
        return variants

    @staticmethod
    @transaction.atomic
    def add_variant(product: Product, variant_data: dict) -> ProductVariant:
        variant_data = dict(variant_data)
        attribute_value_ids = variant_data.pop("attribute_value_ids", [])
        variant = ProductVariant.objects.create(
            product=product, is_default=False, **variant_data
        )
        for attribute_value_id in attribute_value_ids:
            VariantAttributeValue.objects.create(
                variant=variant, attribute_value_id=attribute_value_id
            )
        return variant


class StockService:
    """Único punto de entrada para modificar Stock. Toda alteración genera
    su InventoryMovement en la MISMA transacción atómica que actualiza
    Stock, con select_for_update() sobre la fila de stock -dos ajustes
    simultáneos sobre la misma variante+almacén nunca pueden dejar el saldo
    inconsistente, uno espera al otro (Esquema Backend §5.2)."""

    @staticmethod
    @transaction.atomic
    def adjust_stock(
        *,
        variant: ProductVariant,
        warehouse: Warehouse,
        counted_quantity: Decimal,
        concept: str,
        user,
    ) -> InventoryMovement:
        # get_or_create resuelve la carrera de "primer ajuste sobre esta
        # variante+almacen" (reintenta el get si otro request gano la
        # creacion primero, gracias al constraint unico); el select_for_update
        # posterior es el que de verdad serializa dos ajustes concurrentes
        # sobre una fila YA existente.
        stock, _ = Stock.objects.get_or_create(
            variant=variant, warehouse=warehouse, defaults={"quantity": 0}
        )
        stock = Stock.objects.select_for_update().get(pk=stock.pk)

        delta = counted_quantity - stock.quantity
        if delta == 0:
            raise ValidationError(
                "La cantidad contada es igual al stock actual -no hay nada que ajustar."
            )

        movement = InventoryMovement.objects.create(
            variant=variant,
            warehouse=warehouse,
            user=user,
            type="IN" if delta > 0 else "OUT",
            quantity=abs(delta),
            concept=concept,
            resulting_balance=counted_quantity,
        )
        stock.quantity = counted_quantity
        stock.save(update_fields=["quantity"])

        from usuarios.services import AuditLogService

        AuditLogService.log_action(
            user=user,
            action="STOCK_ADJUSTED",
            entity="Stock",
            entity_id=stock.id,
            details={
                "variant_id": variant.id,
                "warehouse_id": warehouse.id,
                "delta": str(delta),
                "resulting_balance": str(counted_quantity),
                "concept": concept,
            },
        )
        return movement
