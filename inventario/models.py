from django.contrib.postgres.indexes import GinIndex
from django.contrib.postgres.search import SearchVectorField
from django.db import models

from core.models import SoftDeleteModel
from usuarios.models import User


class Warehouse(SoftDeleteModel):
    name = models.CharField(max_length=150)
    address = models.CharField(max_length=255, blank=True)
    is_active = models.BooleanField(default=True)
    deleted_by = models.ForeignKey(
        User, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "warehouses"

    def __str__(self):
        return self.name


class Category(SoftDeleteModel):
    name = models.CharField(max_length=150)
    is_active = models.BooleanField(default=True)
    # Que atributo agrupa filas en la tabla de Productos del frontend PARA
    # LOS PRODUCTOS DE ESTA CATEGORIA (ej. Talla en "Ropa", Talla numerica
    # en "Calzado", ninguno en "Abarrotes") -a proposito por categoria y no
    # uno solo global: un ERP generico vende mas de un tipo de producto a
    # la vez y cada uno necesita su propio criterio de agrupacion, o
    # ninguno. SET_NULL: borrar el Attribute no debe borrar la categoria.
    primary_attribute = models.ForeignKey(
        "Attribute", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    # Atributos que pueden utilizar las variantes de productos de esta
    # categoría. El atributo principal es solo el criterio de agrupación;
    # esta relación define la matriz completa permitida (p. ej. talla y
    # color para Ropa).
    allowed_attributes = models.ManyToManyField(
        "Attribute", blank=True, related_name="categories"
    )
    deleted_by = models.ForeignKey(
        User, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )

    class Meta:
        db_table = "categories"
        verbose_name_plural = "categories"

    def __str__(self):
        return self.name


class Brand(SoftDeleteModel):
    name = models.CharField(max_length=150)
    is_active = models.BooleanField(default=True)
    deleted_by = models.ForeignKey(
        User, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )

    class Meta:
        db_table = "brands"

    def __str__(self):
        return self.name


class Supplier(SoftDeleteModel):
    ruc_or_dni = models.CharField(max_length=20, unique=True)
    company_name = models.CharField(max_length=150)
    phone = models.CharField(max_length=30, blank=True)
    address = models.CharField(max_length=255, blank=True)
    deleted_by = models.ForeignKey(
        User, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )

    class Meta:
        db_table = "suppliers"

    def __str__(self):
        return self.company_name


class Product(SoftDeleteModel):
    type = models.CharField(
        max_length=10,
        choices=[("PRODUCT", "PRODUCT"), ("SERVICE", "SERVICE"), ("ASSET", "ASSET")],
    )
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    category = models.ForeignKey(
        Category, on_delete=models.PROTECT, related_name="products"
    )
    brand = models.ForeignKey(
        Brand,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="products",
    )
    supplier = models.ForeignKey(
        Supplier,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="products",
    )
    unit_of_measure = models.CharField(
        max_length=10, choices=[("UND", "UND"), ("KG", "KG")]
    )
    is_for_sale = models.BooleanField(default=True)
    is_active = models.BooleanField(default=True)
    search_vector = SearchVectorField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)
    deleted_by = models.ForeignKey(
        User, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "products"
        indexes = [
            models.Index(fields=["updated_at"]),
            GinIndex(fields=["search_vector"]),
        ]
        constraints = [
            models.CheckConstraint(
                check=models.Q(type__in=["PRODUCT", "SERVICE", "ASSET"]),
                name="ck_products_type",
            )
        ]

    def __str__(self):
        return self.name


class Attribute(models.Model):
    name = models.CharField(max_length=100)

    class Meta:
        db_table = "attributes"

    def __str__(self):
        return self.name


class AttributeValue(models.Model):
    attribute = models.ForeignKey(
        Attribute, on_delete=models.PROTECT, related_name="values"
    )
    value = models.CharField(max_length=100)

    class Meta:
        db_table = "attribute_values"

    def __str__(self):
        return self.value


class ProductVariant(SoftDeleteModel):
    product = models.ForeignKey(
        Product, on_delete=models.PROTECT, related_name="variants"
    )
    sku = models.CharField(max_length=100, unique=True)
    barcode = models.CharField(max_length=100, unique=True, null=True, blank=True)
    cost = models.DecimalField(max_digits=12, decimal_places=4, default=0)
    price = models.DecimalField(max_digits=12, decimal_places=4, default=0)
    min_stock = models.DecimalField(max_digits=12, decimal_places=3, default=0)
    image_url = models.URLField(null=True, blank=True)
    is_default = models.BooleanField(default=True)
    is_active = models.BooleanField(default=True)
    updated_at = models.DateTimeField(auto_now=True)
    deleted_by = models.ForeignKey(
        User, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )

    class Meta:
        db_table = "product_variants"
        indexes = [
            models.Index(fields=["updated_at"]),
            models.Index(fields=["barcode"]),
        ]

    def __str__(self):
        return self.sku


class VariantAttributeValue(models.Model):
    variant = models.ForeignKey(
        ProductVariant, on_delete=models.CASCADE, related_name="attribute_values"
    )
    attribute_value = models.ForeignKey(
        AttributeValue, on_delete=models.PROTECT, related_name="+"
    )

    class Meta:
        db_table = "variant_attribute_values"
        constraints = [
            models.UniqueConstraint(
                fields=["variant", "attribute_value"], name="uq_variant_attribute_value"
            )
        ]


class ProductPriceHistory(models.Model):
    variant = models.ForeignKey(
        ProductVariant, on_delete=models.PROTECT, related_name="price_history"
    )
    old_cost = models.DecimalField(
        max_digits=12, decimal_places=4, null=True, blank=True
    )
    new_cost = models.DecimalField(
        max_digits=12, decimal_places=4, null=True, blank=True
    )
    old_price = models.DecimalField(
        max_digits=12, decimal_places=4, null=True, blank=True
    )
    new_price = models.DecimalField(
        max_digits=12, decimal_places=4, null=True, blank=True
    )
    changed_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "product_price_history"
        verbose_name_plural = "product price histories"


class VolumePricingTier(models.Model):
    """Tramos de precio por cantidad mínima comprada (Sprint 26, Ficha de
    Producto §5.1) -ej. "por docena", "por caja". SaleService resuelve, por
    línea, el tramo de mayor min_quantity que la cantidad vendida alcance a
    cubrir; si ninguno aplica, se usa ProductVariant.price sin cambios."""

    variant = models.ForeignKey(
        ProductVariant, on_delete=models.CASCADE, related_name="pricing_tiers"
    )
    min_quantity = models.DecimalField(max_digits=12, decimal_places=3)
    unit_price = models.DecimalField(max_digits=12, decimal_places=4)

    class Meta:
        db_table = "volume_pricing_tiers"
        constraints = [
            models.UniqueConstraint(
                fields=["variant", "min_quantity"], name="uq_volume_pricing_tier"
            )
        ]

    def __str__(self):
        return f"{self.variant.sku} x{self.min_quantity} -> {self.unit_price}"


class Stock(models.Model):
    variant = models.ForeignKey(
        ProductVariant, on_delete=models.PROTECT, related_name="stock"
    )
    warehouse = models.ForeignKey(
        Warehouse, on_delete=models.PROTECT, related_name="stock"
    )
    quantity = models.DecimalField(max_digits=12, decimal_places=3, default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "stock"
        verbose_name_plural = "stock"
        constraints = [
            models.UniqueConstraint(
                fields=["variant", "warehouse"], name="uq_stock_variant_warehouse"
            )
        ]


class InventoryMovement(models.Model):
    """Kardex. Particionada nativamente por RANGE sobre created_at (mensual) a nivel de DB;
    el particionado se aplica con una migración manual de SQL, no lo gestiona Django."""

    variant = models.ForeignKey(
        ProductVariant, on_delete=models.PROTECT, related_name="movements"
    )
    warehouse = models.ForeignKey(
        Warehouse, on_delete=models.PROTECT, related_name="movements"
    )
    user = models.ForeignKey(
        User, on_delete=models.PROTECT, related_name="inventory_movements"
    )
    type = models.CharField(max_length=3, choices=[("IN", "IN"), ("OUT", "OUT")])
    quantity = models.DecimalField(max_digits=12, decimal_places=3)
    concept = models.CharField(
        max_length=20,
        choices=[
            ("PURCHASE", "PURCHASE"),
            ("SALE", "SALE"),
            ("ADJUSTMENT", "ADJUSTMENT"),
            ("RETURN", "RETURN"),
            # Sprint 26: traslado de stock entre almacenes del mismo tenant
            # -TRANSFER_OUT en el almacen de origen, TRANSFER_IN en el
            # destino, vinculados entre si via reference_id.
            ("TRANSFER_OUT", "TRANSFER_OUT"),
            ("TRANSFER_IN", "TRANSFER_IN"),
        ],
    )
    reference_id = models.IntegerField(null=True, blank=True)
    oversell_flag = models.BooleanField(default=False)
    # Saldo de Stock.quantity (variant+warehouse) inmediatamente despues de
    # este movimiento. Denormalizado a proposito: reconstruirlo en cada
    # lectura del Kardex requeriria una window function SUM() OVER (...)
    # sobre una tabla particionada potencialmente grande -StockService ya
    # conoce este valor en el momento de escribir, es mas barato guardarlo
    # que recalcularlo (Esquema Backend §5.2).
    resulting_balance = models.DecimalField(max_digits=12, decimal_places=3)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "inventory_movements"
        constraints = [
            models.CheckConstraint(
                check=models.Q(type__in=["IN", "OUT"]),
                name="ck_inventory_movements_type",
            )
        ]


class TaxRate(models.Model):
    """MEJORA 3 (preparatorio): no calcula ni desglosa impuestos todavía."""

    name = models.CharField(max_length=100)
    percentage = models.DecimalField(max_digits=5, decimal_places=2)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "tax_rates"

    def __str__(self):
        return self.name


class ProductTax(models.Model):
    variant = models.ForeignKey(
        ProductVariant, on_delete=models.PROTECT, related_name="taxes"
    )
    tax_rate = models.ForeignKey(TaxRate, on_delete=models.PROTECT, related_name="+")

    class Meta:
        db_table = "product_taxes"
        constraints = [
            models.UniqueConstraint(
                fields=["variant", "tax_rate"], name="uq_product_tax_variant_rate"
            )
        ]


class PurchaseOrder(models.Model):
    """Visible solo si tenant_settings.purchases_enabled = true."""

    supplier = models.ForeignKey(
        Supplier, on_delete=models.PROTECT, related_name="purchase_orders"
    )
    # A que almacen entra el stock al recibir la orden. No estaba en el
    # modelo original -sin el, POST .../receive/ no tiene forma de saber
    # donde aplicar StockService.adjust_stock() (Sprint 5).
    warehouse = models.ForeignKey(
        Warehouse, on_delete=models.PROTECT, related_name="purchase_orders"
    )
    user = models.ForeignKey(
        User, on_delete=models.PROTECT, related_name="purchase_orders"
    )
    invoice_number = models.CharField(max_length=100, blank=True)
    status = models.CharField(
        max_length=15,
        default="PENDING",
        choices=[
            ("PENDING", "PENDING"),
            ("RECEIVED", "RECEIVED"),
            ("CANCELLED", "CANCELLED"),
        ],
    )
    total = models.DecimalField(max_digits=12, decimal_places=4)
    currency = models.CharField(max_length=3, default="PEN")
    received_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "purchase_orders"
        constraints = [
            models.CheckConstraint(
                check=models.Q(status__in=["PENDING", "RECEIVED", "CANCELLED"]),
                name="ck_purchase_orders_status",
            )
        ]


class PurchaseOrderDetail(models.Model):
    purchase_order = models.ForeignKey(
        PurchaseOrder, on_delete=models.CASCADE, related_name="details"
    )
    variant_id = (
        models.IntegerField()
    )  # desacoplado, sin FK física (igual que sale_details)
    quantity = models.DecimalField(max_digits=12, decimal_places=3)
    unit_cost = models.DecimalField(max_digits=12, decimal_places=4)
    subtotal = models.DecimalField(max_digits=12, decimal_places=4)

    class Meta:
        db_table = "purchase_order_details"
