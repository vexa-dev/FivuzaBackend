from django.contrib.auth.hashers import check_password, make_password
from django.db import models
from django_tenants.models import TenantMixin, DomainMixin


class Tenant(TenantMixin):
    company_name = models.CharField(max_length=100)
    ruc = models.CharField(max_length=20, unique=True, null=True, blank=True)
    default_currency = models.CharField(max_length=3, default="PEN")
    status = models.CharField(
        max_length=20,
        choices=[
            ("active", "Active"),
            ("trial", "Trial"),
            ("suspended", "Suspended"),
            ("canceled", "Canceled"),
        ],
        default="trial",
    )
    created_on = models.DateField(auto_now_add=True)

    # Por defecto en True: el esquema se creará y sincronizará automáticamente al guardar
    auto_create_schema = True

    def __str__(self):
        return self.company_name


class Domain(DomainMixin):
    pass


class Plan(models.Model):
    code = models.CharField(max_length=20, unique=True)
    name = models.CharField(max_length=100)
    max_users = models.IntegerField()
    price_monthly = models.DecimalField(max_digits=10, decimal_places=2)
    price_semiannual = models.DecimalField(max_digits=10, decimal_places=2)
    price_annual = models.DecimalField(max_digits=10, decimal_places=2)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "plans"

    def __str__(self):
        return self.name


class PlanFeature(models.Model):
    plan = models.ForeignKey(Plan, on_delete=models.CASCADE, related_name="features")
    feature_code = models.CharField(
        max_length=50,
        choices=[
            ("HAS_SALES_MODULE", "HAS_SALES_MODULE"),
            ("HAS_PURCHASES_MODULE", "HAS_PURCHASES_MODULE"),
            ("HAS_VARIANTS", "HAS_VARIANTS"),
            ("HAS_MULTI_WAREHOUSE", "HAS_MULTI_WAREHOUSE"),
            ("HAS_HR_MODULE", "HAS_HR_MODULE"),
            ("HAS_CASH_MODULE", "HAS_CASH_MODULE"),
        ],
    )
    is_enabled = models.BooleanField(default=True)

    class Meta:
        db_table = "plan_features"
        constraints = [
            models.UniqueConstraint(
                fields=["plan", "feature_code"], name="uq_plan_feature"
            )
        ]


class Subscription(models.Model):
    tenant = models.ForeignKey(
        Tenant, on_delete=models.PROTECT, related_name="subscriptions"
    )
    plan = models.ForeignKey(
        Plan, on_delete=models.PROTECT, related_name="subscriptions"
    )
    billing_cycle = models.CharField(
        max_length=20,
        choices=[
            ("MONTHLY", "MONTHLY"),
            ("SEMIANNUAL", "SEMIANNUAL"),
            ("ANNUAL", "ANNUAL"),
        ],
    )
    price_paid = models.DecimalField(max_digits=10, decimal_places=2)
    currency = models.CharField(max_length=3, default="PEN")
    status = models.CharField(
        max_length=20,
        choices=[
            ("active", "active"),
            ("past_due", "past_due"),
            ("canceled", "canceled"),
        ],
    )
    starts_at = models.DateTimeField()
    expires_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "subscriptions"
        constraints = [
            models.CheckConstraint(
                check=models.Q(status__in=["active", "past_due", "canceled"]),
                name="ck_subscriptions_status",
            )
        ]


class SubscriptionPayment(models.Model):
    subscription = models.ForeignKey(
        Subscription, on_delete=models.PROTECT, related_name="payments"
    )
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    currency = models.CharField(max_length=3, default="PEN")
    payment_method = models.CharField(
        max_length=20,
        choices=[
            ("CARD", "CARD"),
            ("TRANSFER", "TRANSFER"),
            ("YAPE", "YAPE"),
            ("PLIN", "PLIN"),
        ],
    )
    external_reference = models.CharField(max_length=100, blank=True)
    status = models.CharField(
        max_length=20,
        choices=[
            ("PAID", "PAID"),
            ("FAILED", "FAILED"),
            ("REFUNDED", "REFUNDED"),
            ("PENDING", "PENDING"),
        ],
    )
    paid_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "subscription_payments"
        constraints = [
            models.CheckConstraint(
                check=models.Q(status__in=["PAID", "FAILED", "REFUNDED", "PENDING"]),
                name="ck_subscription_payments_status",
            )
        ]


class TenantSettings(models.Model):
    tenant = models.OneToOneField(
        Tenant, on_delete=models.PROTECT, related_name="settings"
    )
    purchases_enabled = models.BooleanField(default=True)
    variants_enabled = models.BooleanField(default=False)
    multi_warehouse_enabled = models.BooleanField(default=False)
    hr_module_enabled = models.BooleanField(default=False)
    cash_module_enabled = models.BooleanField(default=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "tenant_settings"


class PlatformStaff(models.Model):
    """Personal interno de Fivuza (soporte/admin/billing), separado de tenant.users."""

    email = models.EmailField(unique=True)
    password = models.CharField(max_length=255)
    full_name = models.CharField(max_length=150)
    role = models.CharField(
        max_length=20,
        choices=[
            ("SUPER_ADMIN", "SUPER_ADMIN"),
            ("SUPPORT", "SUPPORT"),
            ("BILLING", "BILLING"),
        ],
    )
    is_active = models.BooleanField(default=True)
    last_login = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    # No hereda de AbstractUser -flujo de autenticacion completamente separado
    # de tenant.users (Esquema Backend, mejora de arquitectura #2). is_authenticated
    # e is_anonymous son lo minimo que DRF/permissions necesitan para tratar esta
    # instancia como "el usuario autenticado" de request.user.
    is_authenticated = True
    is_anonymous = False

    class Meta:
        db_table = "platform_staff"
        constraints = [
            models.CheckConstraint(
                check=models.Q(role__in=["SUPER_ADMIN", "SUPPORT", "BILLING"]),
                name="ck_platform_staff_role",
            )
        ]

    def __str__(self):
        return self.email

    def set_password(self, raw_password):
        self.password = make_password(raw_password)

    def check_password(self, raw_password):
        return check_password(raw_password, self.password)
