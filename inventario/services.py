import csv
import io
import uuid
from decimal import Decimal, InvalidOperation

import boto3
from barcode import Code128
from barcode.writer import ImageWriter, SVGWriter
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Sum
from django.template.loader import render_to_string
from django.utils import timezone

from inventario.models import (
    Category,
    InventoryMovement,
    Product,
    ProductPriceHistory,
    ProductVariant,
    PurchaseOrder,
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
        oversell_flag: bool = False,
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
            # Sprint 20: una venta sincronizada offline con stock ya
            # insuficiente igual se registra (el producto ya salio
            # fisicamente de la tienda) -oversell_flag marca el movimiento
            # para revision del dueño en vez de bloquear la venta.
            oversell_flag=oversell_flag,
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

    @staticmethod
    @transaction.atomic
    def transfer_stock(
        *,
        variant: ProductVariant,
        from_warehouse: Warehouse,
        to_warehouse: Warehouse,
        quantity: Decimal,
        user,
    ) -> tuple[InventoryMovement, InventoryMovement]:
        """Traslado de stock entre dos almacenes del mismo tenant (Sprint 26,
        Ficha de Producto §5.1): no pasa por una venta ni una compra. Se
        implementa como dos llamadas a adjust_stock() dentro de la misma
        transacción -cada una ya trae su propio select_for_update(), asi
        que la seguridad de concurrencia es la misma que un ajuste
        individual, solo que ahora sobre dos filas de Stock en vez de una."""
        if from_warehouse.pk == to_warehouse.pk:
            raise ValidationError("El almacén de origen y destino deben ser distintos.")
        if quantity <= 0:
            raise ValidationError("La cantidad a trasladar debe ser mayor a cero.")

        origin_stock, _ = Stock.objects.get_or_create(
            variant=variant, warehouse=from_warehouse, defaults={"quantity": 0}
        )
        origin_stock = Stock.objects.select_for_update().get(pk=origin_stock.pk)
        if origin_stock.quantity < quantity:
            raise ValidationError(
                "No hay stock suficiente en el almacén de origen para este traslado."
            )

        out_movement = StockService.adjust_stock(
            variant=variant,
            warehouse=from_warehouse,
            counted_quantity=origin_stock.quantity - quantity,
            concept="TRANSFER_OUT",
            user=user,
        )

        dest_stock, _ = Stock.objects.get_or_create(
            variant=variant, warehouse=to_warehouse, defaults={"quantity": 0}
        )
        dest_stock = Stock.objects.select_for_update().get(pk=dest_stock.pk)
        in_movement = StockService.adjust_stock(
            variant=variant,
            warehouse=to_warehouse,
            counted_quantity=dest_stock.quantity + quantity,
            concept="TRANSFER_IN",
            user=user,
        )

        # Se vinculan entre si via reference_id -recien aqui, porque cada
        # movimiento necesita el id del otro ya generado.
        out_movement.reference_id = in_movement.id
        out_movement.save(update_fields=["reference_id"])
        in_movement.reference_id = out_movement.id
        in_movement.save(update_fields=["reference_id"])

        return out_movement, in_movement


class PurchaseService:
    """Recibir una orden de compra es la unica forma en que sus lineas
    tocan Stock -siempre via StockService (nunca escribe Stock directo),
    para heredar el mismo lock/auditoria/Kardex ya probado (Esquema
    Backend §5.2)."""

    @staticmethod
    @transaction.atomic
    def receive_order(*, purchase_order: PurchaseOrder, user) -> PurchaseOrder:
        if purchase_order.status != "PENDING":
            raise ValidationError(
                "Solo se puede recibir una orden de compra en estado PENDING."
            )

        for detail in purchase_order.details.all():
            variant = ProductVariant.objects.select_for_update().get(
                id=detail.variant_id
            )
            stock = (
                Stock.objects.select_for_update()
                .filter(variant=variant, warehouse=purchase_order.warehouse)
                .first()
            )
            current_quantity = stock.quantity if stock else Decimal("0")

            # El costo promedio se recalcula ANTES de tocar Stock -el
            # aggregate de abajo debe leer el stock total previo a este
            # ingreso, o el calculo cuenta la cantidad entrante dos veces.
            PurchaseService._update_weighted_average_cost(
                variant=variant,
                incoming_quantity=detail.quantity,
                incoming_unit_cost=detail.unit_cost,
                user=user,
            )
            StockService.adjust_stock(
                variant=variant,
                warehouse=purchase_order.warehouse,
                counted_quantity=current_quantity + detail.quantity,
                concept="PURCHASE",
                user=user,
            )

        purchase_order.status = "RECEIVED"
        purchase_order.received_at = timezone.now()
        purchase_order.save(update_fields=["status", "received_at"])
        return purchase_order

    @staticmethod
    def _update_weighted_average_cost(
        *,
        variant: ProductVariant,
        incoming_quantity: Decimal,
        incoming_unit_cost: Decimal,
        user,
    ) -> None:
        # Promedio ponderado contra el stock TOTAL (todos los almacenes),
        # porque ProductVariant.cost es un unico costo global, no uno por
        # almacen -recibir en cualquier almacen mueve el mismo costo.
        total_before = Stock.objects.filter(variant=variant).aggregate(
            total=Sum("quantity")
        )["total"] or Decimal("0")
        total_after = total_before + incoming_quantity
        if total_after <= 0:
            return

        old_cost = variant.cost
        new_cost = (
            (old_cost * total_before) + (incoming_unit_cost * incoming_quantity)
        ) / total_after
        if new_cost == old_cost:
            return

        ProductPriceHistory.objects.create(
            variant=variant,
            old_cost=old_cost,
            new_cost=new_cost,
            old_price=variant.price,
            new_price=variant.price,
            changed_by=user,
        )
        variant.cost = new_cost
        variant.save(update_fields=["cost"])


_IMPORT_TEMPLATE_HEADERS = [
    "nombre_producto",
    "categoria",
    "sku",
    "codigo_barras",
    "costo",
    "precio",
    "stock_minimo",
    "cantidad_inicial",
    "almacen",
]


class CatalogImportService:
    """Importación masiva de catálogo desde CSV (Sprint 6, hueco #3). Valida
    fila por fila -incluida la detección de códigos de barras duplicados,
    dentro del propio archivo y contra lo que ya existe en la BD- y confirma
    solo las filas válidas; un typo en la fila 300 no debe tirar abajo las
    primeras 299 (cada fila corre en su propio savepoint, no en una unica
    transacción para todo el archivo)."""

    @staticmethod
    def build_template_csv() -> str:
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(_IMPORT_TEMPLATE_HEADERS)
        writer.writerow(
            [
                "Camiseta básica",
                "Ropa",
                "CAM-001",
                "7501234567890",
                "10.00",
                "25.90",
                "5",
                "20",
                "Principal",
            ]
        )
        return buffer.getvalue()

    @staticmethod
    def import_csv(*, file_content: str, user) -> dict:
        reader = csv.DictReader(io.StringIO(file_content))
        missing = set(_IMPORT_TEMPLATE_HEADERS) - set(reader.fieldnames or [])
        if missing:
            raise ValidationError(
                f"Faltan columnas en el archivo: {', '.join(sorted(missing))}"
            )

        rows = list(reader)
        seen_skus: set[str] = set()
        seen_barcodes: set[str] = set()
        results = []

        for index, row in enumerate(rows, start=2):  # la fila 1 es el encabezado
            error = CatalogImportService._validate_row(row, seen_skus, seen_barcodes)
            if error:
                results.append(
                    {
                        "row": index,
                        "sku": row.get("sku", ""),
                        "status": "error",
                        "error": error,
                    }
                )
                continue

            try:
                with transaction.atomic():
                    CatalogImportService._create_row(row, user)
            except Exception as exc:  # noqa: BLE001 -- una fila mala no debe frenar el resto del archivo
                results.append(
                    {
                        "row": index,
                        "sku": row.get("sku", ""),
                        "status": "error",
                        "error": str(exc),
                    }
                )
                continue

            results.append({"row": index, "sku": row["sku"], "status": "created"})

        created = sum(1 for r in results if r["status"] == "created")
        errors = sum(1 for r in results if r["status"] == "error")
        return {
            "total": len(rows),
            "created": created,
            "errors": errors,
            "rows": results,
        }

    @staticmethod
    def _validate_row(row: dict, seen_skus: set, seen_barcodes: set) -> str | None:
        name = (row.get("nombre_producto") or "").strip()
        category_name = (row.get("categoria") or "").strip()
        sku = (row.get("sku") or "").strip()
        barcode = (row.get("codigo_barras") or "").strip()
        warehouse_name = (row.get("almacen") or "").strip()

        if not name:
            return "nombre_producto es requerido"
        if not sku:
            return "sku es requerido"
        if sku in seen_skus:
            return f"sku '{sku}' duplicado en el archivo"
        if ProductVariant.objects.filter(sku=sku).exists():
            return f"sku '{sku}' ya existe en el catálogo"
        if barcode:
            if barcode in seen_barcodes:
                return f"código de barras '{barcode}' duplicado en el archivo"
            if ProductVariant.objects.filter(barcode=barcode).exists():
                return f"código de barras '{barcode}' ya existe en el catálogo"
        if not category_name:
            return "categoria es requerida"
        if not Category.objects.filter(name__iexact=category_name).exists():
            return f"categoria '{category_name}' no existe"

        try:
            cantidad_inicial = Decimal(row.get("cantidad_inicial") or "0")
        except InvalidOperation:
            return "cantidad_inicial invalida"
        if cantidad_inicial > 0 and not warehouse_name:
            return "almacen es requerido si hay cantidad_inicial"
        if (
            warehouse_name
            and not Warehouse.objects.filter(name__iexact=warehouse_name).exists()
        ):
            return f"almacen '{warehouse_name}' no existe"

        seen_skus.add(sku)
        if barcode:
            seen_barcodes.add(barcode)
        return None

    @staticmethod
    def _create_row(row: dict, user) -> None:
        name = row["nombre_producto"].strip()
        category = Category.objects.get(name__iexact=row["categoria"].strip())
        sku = row["sku"].strip()
        barcode = (row.get("codigo_barras") or "").strip() or None

        product = ProductVariantService.create_product(
            product_data={
                "type": "PRODUCT",
                "name": name,
                "category": category,
                "unit_of_measure": "UND",
            },
            variants_data=[
                {
                    "sku": sku,
                    "barcode": barcode,
                    "cost": row.get("costo") or "0",
                    "price": row.get("precio") or "0",
                    "min_stock": row.get("stock_minimo") or "0",
                }
            ],
        )

        cantidad_inicial = Decimal(row.get("cantidad_inicial") or "0")
        warehouse_name = (row.get("almacen") or "").strip()
        if cantidad_inicial > 0 and warehouse_name:
            warehouse = Warehouse.objects.get(name__iexact=warehouse_name)
            variant = product.variants.first()
            StockService.adjust_stock(
                variant=variant,
                warehouse=warehouse,
                counted_quantity=cantidad_inicial,
                concept="ADJUSTMENT",
                user=user,
            )


class LabelService:
    """Etiquetas de código de barras (Sprint 27, Ficha de Producto §5.1):
    imagen cruda del código (PNG/SVG) para uso genérico, y HTML imprimible
    de una hoja de etiquetas en tamaños estándar -mismo patrón que
    ReceiptService (backend arma el HTML, el frontend lo imprime con
    window.print())."""

    SIZE_MM = {
        "40x25": (40, 25),
        "50x30": (50, 30),
    }

    # Ajustado para que el codigo + su texto quepan dentro de una etiqueta
    # de 40mm de ancho sin desbordar (module_width en mm por barra).
    _WRITER_OPTIONS = {
        "module_width": 0.28,
        "module_height": 10.0,
        "quiet_zone": 1.5,
        "font_size": 7,
        "text_distance": 2.5,
        "write_text": True,
    }

    @staticmethod
    def _require_barcode(variant: ProductVariant) -> str:
        if not variant.barcode:
            raise ValidationError("La variante no tiene código de barras asignado.")
        return variant.barcode

    @staticmethod
    def generate_barcode_svg(variant: ProductVariant) -> str:
        data = LabelService._require_barcode(variant)
        code = Code128(data, writer=SVGWriter())
        buffer = io.BytesIO()
        code.write(buffer, options=LabelService._WRITER_OPTIONS)
        return buffer.getvalue().decode("utf-8")

    @staticmethod
    def generate_barcode_png(variant: ProductVariant) -> bytes:
        data = LabelService._require_barcode(variant)
        code = Code128(data, writer=ImageWriter())
        buffer = io.BytesIO()
        code.write(buffer, options=LabelService._WRITER_OPTIONS)
        return buffer.getvalue()

    @staticmethod
    def render_labels_html(items: list[dict], size: str = "40x25") -> str:
        if size not in LabelService.SIZE_MM:
            raise ValidationError("Tamaño de etiqueta no soportado.")
        width_mm, height_mm = LabelService.SIZE_MM[size]

        labels = []
        for item in items:
            variant = item["variant"]
            svg = LabelService.generate_barcode_svg(variant)
            for _ in range(item["quantity"]):
                labels.append(
                    {
                        "product_name": variant.product.name,
                        "sku": variant.sku,
                        "price": variant.price.quantize(Decimal("0.01")),
                        "barcode_svg": svg,
                    }
                )

        return render_to_string(
            "inventario/labels/label_sheet.html",
            {"labels": labels, "width_mm": width_mm, "height_mm": height_mm},
        )
