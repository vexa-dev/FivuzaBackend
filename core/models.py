from django.contrib.auth.hashers import check_password, make_password
from django.db import models
from django_tenants.models import TenantMixin, DomainMixin


class ActiveManager(models.Manager):
    """Manager por defecto de SoftDeleteModel: excluye automaticamente los
    registros con deleted_at no nulo. Todo el codigo de negocio debe usar
    este manager (Model.objects) por defecto; solo una pantalla explicita de
    papelera/auditoria debe usar Model.all_objects (Convenciones de Codigo
    §2.5; Esquema Backend §2.5)."""

    def get_queryset(self):
        return super().get_queryset().filter(deleted_at__isnull=True)


class SoftDeleteModel(models.Model):
    """Modelo abstracto para eliminacion logica, compartido entre apps.

    Cada modelo concreto que herede de esta clase debe declarar su propio
    campo deleted_by -el FK correcto depende de que modelo de usuario aplica
    en su esquema (tenant.users o platform_staff), por eso no se declara aqui.
    Es una excepcion deliberada a la regla de "nunca importar modelos de otra
    app": SoftDeleteModel es un mixin abstracto sin tabla ni relacion propia,
    no una dependencia de negocio entre apps.
    """

    deleted_at = models.DateTimeField(null=True, blank=True)

    objects = ActiveManager()
    all_objects = models.Manager()

    class Meta:
        abstract = True


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
    suspended_at = models.DateTimeField(null=True, blank=True)
    canceled_at = models.DateTimeField(null=True, blank=True)
    created_on = models.DateField(auto_now_add=True)
    provisioning_status = models.CharField(
        max_length=20,
        choices=[
            ("PENDING", "PENDING"),
            ("IN_PROGRESS", "IN_PROGRESS"),
            ("COMPLETED", "COMPLETED"),
            ("FAILED", "FAILED"),
        ],
        default="PENDING",
    )

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


# Catalogo de feature flags compartido entre PlanFeature (a nivel de plan) y
# TenantFeatureOverride (Sprint 10, a nivel de UN tenant especifico) -un solo
# lugar para agregar un feature_code nuevo en vez de mantener dos listas.
FEATURE_CODE_CHOICES = [
    ("HAS_SALES_MODULE", "HAS_SALES_MODULE"),
    ("HAS_PURCHASES_MODULE", "HAS_PURCHASES_MODULE"),
    ("HAS_VARIANTS", "HAS_VARIANTS"),
    ("HAS_MULTI_WAREHOUSE", "HAS_MULTI_WAREHOUSE"),
    ("HAS_HR_MODULE", "HAS_HR_MODULE"),
    ("HAS_CASH_MODULE", "HAS_CASH_MODULE"),
]


class PlanFeature(models.Model):
    plan = models.ForeignKey(Plan, on_delete=models.CASCADE, related_name="features")
    feature_code = models.CharField(
        max_length=50,
        choices=FEATURE_CODE_CHOICES,
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
    # Umbral de diferencia de arqueo (valor absoluto, moneda del tenant) a
    # partir del cual CashSessionService.close_session() dispara el aviso
    # asincrono al administrador (TRD §5.4). No hay un valor "correcto" único
    # documentado -se asume 20.00 como default razonable, ajustable por tenant.
    cash_difference_alert_threshold = models.DecimalField(
        max_digits=12, decimal_places=4, default=20
    )
    # Sprint 24: cada cuantos minutos DashboardRefreshService recalcula las
    # vistas materializadas de este tenant (Esquema Backend §9.2). No hay un
    # valor "correcto" documentado -15 minutos es el piso que la propia fila
    # del plan sugiere ("cada 15-30 min"), ajustable por tenant segun cuanto
    # trafico de ventas tenga.
    dashboard_refresh_minutes = models.IntegerField(default=15)
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


class PlatformAuditLog(models.Model):
    """Bitacora de acciones de platform_staff sobre la plataforma (suspender/
    reactivar un tenant, confirmar un pago, crear/editar un plan, etc.).
    Complementa a tenant.audit_logs, que solo registra acciones DENTRO del
    negocio de un tenant. Se escribe unicamente via PlatformAuditLogService,
    nunca por POST/PUT directo del cliente (BDD v5, seccion public.platform_audit_logs)."""

    platform_staff = models.ForeignKey(
        PlatformStaff, on_delete=models.PROTECT, related_name="audit_logs"
    )
    action = models.CharField(max_length=100)
    entity = models.CharField(max_length=100)
    entity_id = models.IntegerField()
    details = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "platform_audit_logs"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.action} on {self.entity}#{self.entity_id}"


class TenantImpersonationSession(models.Model):
    """Sesion de soporte tecnico: platform_staff actua temporalmente como el
    admin de un tenant, sin pedirle su contraseña (Especificacion de API
    §4.24; Ficha de Producto §6). Se cierra sola a los 60 minutos
    (expires_at) o antes, si platform_staff o el propio usuario impersonado
    la termina explicitamente (ended_at)."""

    tenant = models.ForeignKey(
        Tenant, on_delete=models.PROTECT, related_name="impersonation_sessions"
    )
    platform_staff = models.ForeignKey(
        PlatformStaff, on_delete=models.PROTECT, related_name="impersonation_sessions"
    )
    reason = models.TextField()
    started_at = models.DateTimeField(auto_now_add=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField()

    class Meta:
        db_table = "tenant_impersonation_sessions"
        ordering = ["-started_at"]


class TenantFeatureOverride(models.Model):
    """Activa/desactiva UNA caracteristica para UN tenant especifico, sin
    tocar su plan contratado (Especificacion de API §4.25; Esquema Backend
    §8.2). Tiene prioridad sobre PlanFeature en FeatureFlagService.is_enabled()
    -a diferencia de PlanFeature/TenantSettings (que solo pueden apagar una
    caracteristica), un override puede tambien PRENDERLA aunque el plan no
    la incluya (ej. dar acceso beta antes de cambiar de plan)."""

    tenant = models.ForeignKey(
        Tenant, on_delete=models.CASCADE, related_name="feature_overrides"
    )
    feature_code = models.CharField(max_length=50, choices=FEATURE_CODE_CHOICES)
    is_enabled = models.BooleanField()

    class Meta:
        db_table = "tenant_feature_overrides"
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "feature_code"], name="uq_tenant_feature_override"
            )
        ]


class TenantNote(models.Model):
    """Nota interna del equipo Fivuza sobre un tenant (Especificacion de API
    §4.25) -nunca visible para el propio negocio, vive enteramente en el
    esquema public junto al resto de la administracion de core."""

    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="notes")
    platform_staff = models.ForeignKey(
        PlatformStaff, on_delete=models.PROTECT, related_name="tenant_notes"
    )
    text = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "tenant_notes"
        ordering = ["-created_at"]


class SubscriptionDiscount(models.Model):
    """Condicion especial de precio negociada con un tenant puntual, sin
    crear un plan nuevo solo para el (Especificacion de API §4.25). Exige
    EXACTAMENTE uno de discount_percent/override_price -nunca ambos, nunca
    ninguno (si no hay descuento, simplemente no se crea la fila)."""

    subscription = models.ForeignKey(
        Subscription, on_delete=models.CASCADE, related_name="discounts"
    )
    discount_percent = models.DecimalField(
        max_digits=5, decimal_places=2, null=True, blank=True
    )
    override_price = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True
    )
    reason = models.TextField()
    expires_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "subscription_discounts"
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(
                check=(
                    models.Q(
                        discount_percent__isnull=False, override_price__isnull=True
                    )
                    | models.Q(
                        discount_percent__isnull=True, override_price__isnull=False
                    )
                ),
                name="ck_subscription_discount_exactly_one_type",
            )
        ]

    def is_active(self) -> bool:
        from django.utils import timezone

        return self.expires_at is None or self.expires_at > timezone.now()
