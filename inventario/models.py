from django.contrib.postgres.indexes import GinIndex
from django.contrib.postgres.search import SearchVectorField
from django.db import models

from usuarios.models import User


class Warehouse(models.Model):
    name = models.CharField(max_length=150)
    address = models.CharField(max_length=255, blank=True)
    is_active = models.BooleanField(default=True)
    deleted_at = models.DateTimeField(null=True, blank=True)
    deleted_by = models.ForeignKey(
        User, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "warehouses"

    def __str__(self):
        return self.name


class Category(models.Model):
    name = models.CharField(max_length=150)
    is_active = models.BooleanField(default=True)
    deleted_at = models.DateTimeField(null=True, blank=True)
    deleted_by = models.ForeignKey(
        User, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )

    class Meta:
        db_table = "categories"
        verbose_name_plural = "categories"

    def __str__(self):
        return self.name


class Supplier(models.Model):
    ruc_or_dni = models.CharField(max_length=20, unique=True)
    company_name = models.CharField(max_length=150)
    phone = models.CharField(max_length=30, blank=True)
    address = models.CharField(max_length=255, blank=True)
    deleted_at = models.DateTimeField(null=True, blank=True)
    deleted_by = models.ForeignKey(
        User, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )

    class Meta:
        db_table = "suppliers"

    def __str__(self):
        return self.company_name


class Product(models.Model):
    type = models.CharField(
        max_length=10,
        choices=[("PRODUCT", "PRODUCT"), ("SERVICE", "SERVICE"), ("ASSET", "ASSET")],
    )
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    category = models.ForeignKey(Category, on_delete=models.PROTECT, related_name="products")
    supplier = models.ForeignKey(
        Supplier, on_delete=models.PROTECT, null=True, blank=True, related_name="products"
    )
    unit_of_measure = models.CharField(
        max_length=10, choices=[("UND", "UND"), ("KG", "KG")]
    )
    is_for_sale = models.BooleanField(default=True)
    is_active = models.BooleanField(default=True)
    search_vector = SearchVectorField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)
    deleted_at = models.DateTimeField(null=True, blank=True)
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
    attribute = models.ForeignKey(Attribute, on_delete=models.PROTECT, related_name="values")
    value = models.CharField(max_length=100)

    class Meta:
        db_table = "attribute_values"

    def __str__(self):
        return self.value


class ProductVariant(models.Model):
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="variants")
    sku = models.CharField(max_length=100, unique=True)
    cost = models.DecimalField(max_digits=12, decimal_places=4, default=0)
    price = models.DecimalField(max_digits=12, decimal_places=4, default=0)
    min_stock = models.DecimalField(max_digits=12, decimal_places=3, default=0)
    image_url = models.URLField(null=True, blank=True)
    is_default = models.BooleanField(default=True)
    is_active = models.BooleanField(default=True)
    updated_at = models.DateTimeField(auto_now=True)
    deleted_at = models.DateTimeField(null=True, blank=True)
    deleted_by = models.ForeignKey(
        User, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )

    class Meta:
        db_table = "product_variants"
        indexes = [models.Index(fields=["updated_at"])]

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
    old_cost = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True)
    new_cost = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True)
    old_price = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True)
    new_price = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True)
    changed_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "product_price_history"
        verbose_name_plural = "product price histories"


class Stock(models.Model):
    variant = models.ForeignKey(ProductVariant, on_delete=models.PROTECT, related_name="stock")
    warehouse = models.ForeignKey(Warehouse, on_delete=models.PROTECT, related_name="stock")
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

    variant = models.ForeignKey(ProductVariant, on_delete=models.PROTECT, related_name="movements")
    warehouse = models.ForeignKey(Warehouse, on_delete=models.PROTECT, related_name="movements")
    user = models.ForeignKey(User, on_delete=models.PROTECT, related_name="inventory_movements")
    type = models.CharField(max_length=3, choices=[("IN", "IN"), ("OUT", "OUT")])
    quantity = models.DecimalField(max_digits=12, decimal_places=3)
    concept = models.CharField(
        max_length=20,
        choices=[
            ("PURCHASE", "PURCHASE"),
            ("SALE", "SALE"),
            ("ADJUSTMENT", "ADJUSTMENT"),
            ("RETURN", "RETURN"),
        ],
    )
    reference_id = models.IntegerField(null=True, blank=True)
    oversell_flag = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "inventory_movements"
        constraints = [
            models.CheckConstraint(
                check=models.Q(type__in=["IN", "OUT"]), name="ck_inventory_movements_type"
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
    variant = models.ForeignKey(ProductVariant, on_delete=models.PROTECT, related_name="taxes")
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

    supplier = models.ForeignKey(Supplier, on_delete=models.PROTECT, related_name="purchase_orders")
    user = models.ForeignKey(User, on_delete=models.PROTECT, related_name="purchase_orders")
    invoice_number = models.CharField(max_length=100, blank=True)
    status = models.CharField(
        max_length=15,
        choices=[
            ("PENDING", "PENDING"),
            ("RECEIVED", "RECEIVED"),
            ("CANCELLED", "CANCELLED"),
        ],
    )
    total = models.DecimalField(max_digits=12, decimal_places=4)
    currency = models.CharField(max_length=3, default="PEN")
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
    variant_id = models.IntegerField()  # desacoplado, sin FK física (igual que sale_details)
    quantity = models.DecimalField(max_digits=12, decimal_places=3)
    unit_cost = models.DecimalField(max_digits=12, decimal_places=4)
    subtotal = models.DecimalField(max_digits=12, decimal_places=4)

    class Meta:
        db_table = "purchase_order_details"
