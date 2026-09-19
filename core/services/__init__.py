"""Servicios de core divididos por dominio. Se re-exporta todo aqui para
que `from core.services import X` siga funcionando (incluidas las
migraciones de datos que importan TenantProvisioningService)."""

from core.services.tenants import (
    DATA_RETENTION_GRACE_DAYS,
    IMPERSONATION_SESSION_MINUTES,
    TenantLifecycleService,
    TenantProvisioningService,
    TenantRegistrationService,
    _BILLING_CYCLE_DAYS,
    _BILLING_CYCLE_PRICE_FIELD,
)
from core.services.billing import (
    InvalidDiscountError,
    PaymentAlreadyConfirmedError,
    SubscriptionDiscountService,
    SubscriptionPaymentService,
)
from core.services.features import (
    FeatureFlagService,
    TenantFeatureOverrideService,
)
from core.services.platform import (
    PlatformAuditLogService,
    PlatformDashboardService,
)
from core.services.impersonation import (
    NoImpersonableAdminUserError,
    TenantImpersonationService,
)
from core.services.tenant_admin import (
    TenantConsumptionService,
    TenantHealthService,
    TenantNoteService,
    TenantOnboardingService,
)
from core.services.demo import (
    DemoTenantService,
    TenantNotDemoError,
)
from core.services.retention import (
    TenantDataRetentionService,
)

__all__ = [
    "DATA_RETENTION_GRACE_DAYS",
    "DemoTenantService",
    "FeatureFlagService",
    "IMPERSONATION_SESSION_MINUTES",
    "InvalidDiscountError",
    "NoImpersonableAdminUserError",
    "PaymentAlreadyConfirmedError",
    "PlatformAuditLogService",
    "PlatformDashboardService",
    "SubscriptionDiscountService",
    "SubscriptionPaymentService",
    "TenantConsumptionService",
    "TenantDataRetentionService",
    "TenantFeatureOverrideService",
    "TenantHealthService",
    "TenantImpersonationService",
    "TenantLifecycleService",
    "TenantNotDemoError",
    "TenantNoteService",
    "TenantOnboardingService",
    "TenantProvisioningService",
    "TenantRegistrationService",
    "_BILLING_CYCLE_DAYS",
    "_BILLING_CYCLE_PRICE_FIELD",
]
