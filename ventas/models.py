from django.contrib.postgres.indexes import GinIndex
from django.contrib.postgres.search import SearchVectorField
from django.db import models

from inventario.models import Category, ProductVariant, Warehouse
from usuarios.models import User


class CashRegister(models.Model):
    warehouse = models.ForeignKey(
        Warehouse, on_delete=models.PROTECT, related_name="cash_registers"
    )
    name = models.CharField(max_length=100)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "cash_registers"

    def __str__(self):
        return self.name


class CashSession(models.Model):
    cash_register = models.ForeignKey(
        CashRegister, on_delete=models.PROTECT, related_name="sessions"
    )
    user = models.ForeignKey(
        User, on_delete=models.PROTECT, related_name="cash_sessions"
    )
    opening_amount = models.DecimalField(max_digits=12, decimal_places=4)
    opening_at = models.DateTimeField()
    expected_closing_amount = models.DecimalField(
        max_digits=12, decimal_places=4, null=True, blank=True
    )
    counted_closing_amount = models.DecimalField(
        max_digits=12, decimal_places=4, null=True, blank=True
    )
    difference = models.DecimalField(
        max_digits=12, decimal_places=4, null=True, blank=True
    )
    status = models.CharField(
        max_length=10, choices=[("OPEN", "OPEN"), ("CLOSED", "CLOSED")]
    )
    closing_at = models.DateTimeField(null=True, blank=True)
    notes = models.CharField(max_length=255, null=True, blank=True)

    class Meta:
        db_table = "cash_sessions"
        constraints = [
            models.CheckConstraint(
                check=models.Q(status__in=["OPEN", "CLOSED"]),
                name="ck_cash_sessions_status",
            )
        ]


class CashMovement(models.Model):
    cash_session = models.ForeignKey(
        CashSession, on_delete=models.PROTECT, related_name="movements"
    )
    type = models.CharField(max_length=3, choices=[("IN", "IN"), ("OUT", "OUT")])
    concept = models.CharField(
        max_length=30,
        choices=[
            ("RETIRO", "RETIRO"),
            ("PAGO_PROVEEDOR_EFECTIVO", "PAGO_PROVEEDOR_EFECTIVO"),
            ("DEPOSITO_BANCO", "DEPOSITO_BANCO"),
            ("AJUSTE", "AJUSTE"),
        ],
    )
    amount = models.DecimalField(max_digits=12, decimal_places=4)
    user = models.ForeignKey(
        User, on_delete=models.PROTECT, related_name="cash_movements"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "cash_movements"
        constraints = [
            models.CheckConstraint(
                check=models.Q(type__in=["IN", "OUT"]), name="ck_cash_movements_type"
            )
        ]


class Customer(models.Model):
    document_type = models.CharField(
        max_length=10, choices=[("RUC", "RUC"), ("DNI", "DNI"), ("ANONIMO", "ANONIMO")]
    )
    document_number = models.CharField(max_length=20, unique=True)
    name = models.CharField(max_length=200)
    phone = models.CharField(max_length=30, blank=True)
    address = models.CharField(max_length=255, blank=True)
    is_active = models.BooleanField(default=True)
    search_vector = SearchVectorField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)
    deleted_at = models.DateTimeField(null=True, blank=True)
    deleted_by = models.ForeignKey(
        User, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "customers"
        indexes = [
            models.Index(fields=["updated_at"]),
            GinIndex(fields=["search_vector"]),
        ]
        constraints = [
            models.CheckConstraint(
                check=models.Q(document_type__in=["RUC", "DNI", "ANONIMO"]),
                name="ck_customers_document_type",
            )
        ]

    def __str__(self):
        return self.name


class Promotion(models.Model):
    name = models.CharField(max_length=150)
    type = models.CharField(
        max_length=20,
        choices=[("PERCENTAGE", "PERCENTAGE"), ("FIXED_AMOUNT", "FIXED_AMOUNT")],
    )
    value = models.DecimalField(max_digits=12, decimal_places=4)
    start_date = models.DateTimeField()
    end_date = models.DateTimeField()
    is_active = models.BooleanField(default=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "promotions"
        indexes = [models.Index(fields=["updated_at"])]
        constraints = [
            models.CheckConstraint(
                check=models.Q(type__in=["PERCENTAGE", "FIXED_AMOUNT"]),
                name="ck_promotions_type",
            )
        ]

    def __str__(self):
        return self.name


class PromotionProduct(models.Model):
    """Uno de variant / category va poblado, no ambos."""

    promotion = models.ForeignKey(
        Promotion, on_delete=models.CASCADE, related_name="targets"
    )
    variant = models.ForeignKey(
        ProductVariant,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="+",
    )
    category = models.ForeignKey(
        Category, on_delete=models.CASCADE, null=True, blank=True, related_name="+"
    )

    class Meta:
        db_table = "promotion_products"


class Sale(models.Model):
    invoice_number = models.CharField(max_length=50, unique=True)
    customer = models.ForeignKey(
        Customer, on_delete=models.PROTECT, related_name="sales"
    )
    user = models.ForeignKey(User, on_delete=models.PROTECT, related_name="sales")
    warehouse = models.ForeignKey(
        Warehouse, on_delete=models.PROTECT, related_name="sales"
    )
    cash_session = models.ForeignKey(
        CashSession,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="sales",
    )
    subtotal = models.DecimalField(max_digits=12, decimal_places=4)
    discount_total = models.DecimalField(max_digits=12, decimal_places=4, default=0)
    total = models.DecimalField(max_digits=12, decimal_places=4)
    currency = models.CharField(max_length=3, default="PEN")
    payment_status = models.CharField(
        max_length=10,
        choices=[("PAID", "PAID"), ("PARTIAL", "PARTIAL"), ("UNPAID", "UNPAID")],
    )
    status = models.CharField(
        max_length=15,
        choices=[
            ("COMPLETED", "COMPLETED"),
            ("VOIDED", "VOIDED"),
            ("CANCELLED", "CANCELLED"),
        ],
    )
    client_side_uuid = models.CharField(max_length=64, unique=True)
    sync_status = models.CharField(
        max_length=20,
        choices=[
            ("SYNCED", "SYNCED"),
            ("OFFLINE_PENDING", "OFFLINE_PENDING"),
            ("CONFLICT", "CONFLICT"),
        ],
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "sales"
        constraints = [
            models.CheckConstraint(
                check=models.Q(payment_status__in=["PAID", "PARTIAL", "UNPAID"]),
                name="ck_sales_payment_status",
            ),
            models.CheckConstraint(
                check=models.Q(status__in=["COMPLETED", "VOIDED", "CANCELLED"]),
                name="ck_sales_status",
            ),
            models.CheckConstraint(
                check=models.Q(
                    sync_status__in=["SYNCED", "OFFLINE_PENDING", "CONFLICT"]
                ),
                name="ck_sales_sync_status",
            ),
        ]

    def __str__(self):
        return self.invoice_number


class SalePayment(models.Model):
    sale = models.ForeignKey(Sale, on_delete=models.CASCADE, related_name="payments")
    method = models.CharField(
        max_length=20,
        choices=[
            ("CASH", "CASH"),
            ("CARD", "CARD"),
            ("YAPE", "YAPE"),
            ("CREDIT_LEDGER", "CREDIT_LEDGER"),
            ("BALANCE", "BALANCE"),
        ],
    )
    amount = models.DecimalField(max_digits=12, decimal_places=4)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "sale_payments"
        constraints = [
            models.CheckConstraint(
                check=models.Q(
                    method__in=["CASH", "CARD", "YAPE", "CREDIT_LEDGER", "BALANCE"]
                ),
                name="ck_sale_payments_method",
            )
        ]


class SaleDetail(models.Model):
    """Sin FK física a product_variants (variant_id desacoplado): evita el acoplamiento
    directo entre Ventas e Inventario, según la decisión documentada en la BDD."""

    sale = models.ForeignKey(Sale, on_delete=models.CASCADE, related_name="details")
    variant_id = models.IntegerField()
    product_name_snapshot = models.CharField(max_length=200)
    sku_snapshot = models.CharField(max_length=100)
    quantity = models.DecimalField(max_digits=12, decimal_places=3)
    unit_price = models.DecimalField(max_digits=12, decimal_places=4)
    discount_amount = models.DecimalField(max_digits=12, decimal_places=4, default=0)
    subtotal = models.DecimalField(max_digits=12, decimal_places=4)

    class Meta:
        db_table = "sale_details"


class SaleReturn(models.Model):
    sale = models.ForeignKey(Sale, on_delete=models.PROTECT, related_name="returns")
    user = models.ForeignKey(
        User, on_delete=models.PROTECT, related_name="sale_returns"
    )
    reason = models.CharField(max_length=255, blank=True)
    total_refund_amount = models.DecimalField(max_digits=12, decimal_places=4)
    refund_type = models.CharField(
        max_length=10, choices=[("BALANCE", "BALANCE"), ("CASH", "CASH")]
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "sale_returns"
        constraints = [
            models.CheckConstraint(
                check=models.Q(refund_type__in=["BALANCE", "CASH"]),
                name="ck_sale_returns_refund_type",
            )
        ]


class SaleReturnDetail(models.Model):
    sale_return = models.ForeignKey(
        SaleReturn, on_delete=models.CASCADE, related_name="details"
    )
    sale_detail = models.ForeignKey(
        SaleDetail, on_delete=models.PROTECT, related_name="return_details"
    )
    quantity_returned = models.DecimalField(max_digits=12, decimal_places=3)
    restock = models.BooleanField(default=True)  # False para ítems tipo SERVICE
    subtotal = models.DecimalField(max_digits=12, decimal_places=4)

    class Meta:
        db_table = "sale_return_details"


class CustomerDebtLedger(models.Model):
    """Libro exclusivo del flujo de ventas al crédito ('fiado')."""

    customer = models.ForeignKey(
        Customer, on_delete=models.PROTECT, related_name="debt_ledger"
    )
    sale = models.ForeignKey(
        Sale,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="debt_entries",
    )
    type = models.CharField(
        max_length=6, choices=[("DEBIT", "DEBIT"), ("CREDIT", "CREDIT")]
    )
    amount = models.DecimalField(max_digits=12, decimal_places=4)
    currency = models.CharField(max_length=3, default="PEN")
    description = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "customer_debt_ledger"
        constraints = [
            models.CheckConstraint(
                check=models.Q(type__in=["DEBIT", "CREDIT"]),
                name="ck_customer_debt_ledger_type",
            )
        ]


class CustomerBalanceLedger(models.Model):
    """Libro exclusivo del saldo a favor generado por devoluciones."""

    customer = models.ForeignKey(
        Customer, on_delete=models.PROTECT, related_name="balance_ledger"
    )
    sale_return = models.ForeignKey(
        SaleReturn,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="balance_entries",
    )
    sale = models.ForeignKey(
        Sale,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="balance_entries",
    )
    type = models.CharField(
        max_length=6, choices=[("CREDIT", "CREDIT"), ("DEBIT", "DEBIT")]
    )
    amount = models.DecimalField(max_digits=12, decimal_places=4)
    currency = models.CharField(max_length=3, default="PEN")
    description = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "customer_balance_ledger"
        constraints = [
            models.CheckConstraint(
                check=models.Q(type__in=["CREDIT", "DEBIT"]),
                name="ck_customer_balance_ledger_type",
            )
        ]
