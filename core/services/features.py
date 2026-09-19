from core.models import (
    PlanFeature,
    Tenant,
    TenantFeatureOverride,
    TenantSettings,
)


class FeatureFlagService:
    """Bloquea el acceso a funcionalidad opcional segun dos capas
    independientes: TenantSettings (el interruptor que el propio negocio
    prende/apaga, ej. "activar variantes") y PlanFeature (el techo que le
    pone su plan de suscripcion). Ambas capas deben permitirlo -Esquema
    Backend §8.2. Se adelanta al Sprint 3 porque variants_enabled y
    multi_warehouse_enabled ya aplican a inventario desde este sprint."""

    _TENANT_SETTINGS_FIELDS = {
        "HAS_VARIANTS": "variants_enabled",
        "HAS_MULTI_WAREHOUSE": "multi_warehouse_enabled",
        "HAS_HR_MODULE": "hr_module_enabled",
        "HAS_CASH_MODULE": "cash_module_enabled",
        "HAS_PURCHASES_MODULE": "purchases_enabled",
        "HAS_GYM_MODULE": "gym_module_enabled",
    }

    @staticmethod
    def is_enabled(tenant: Tenant, feature_code: str) -> bool:
        # Sprint 10 (Especificacion de API §4.25): un override individual
        # tiene prioridad sobre todo lo demas -a diferencia de
        # TenantSettings/PlanFeature (que solo pueden apagar), un override
        # tambien puede PRENDER una caracteristica que el plan no incluye,
        # por eso corta la evaluacion aqui en vez de sumarse a las demas capas.
        override = TenantFeatureOverride.objects.filter(
            tenant=tenant, feature_code=feature_code
        ).first()
        if override is not None:
            return override.is_enabled

        settings_field = FeatureFlagService._TENANT_SETTINGS_FIELDS.get(feature_code)
        if settings_field:
            settings = TenantSettings.objects.filter(tenant=tenant).first()
            if settings is not None and not getattr(settings, settings_field):
                return False

        plan_feature = (
            PlanFeature.objects.filter(
                plan__subscriptions__tenant=tenant,
                plan__subscriptions__status="active",
                feature_code=feature_code,
            )
            .order_by("-id")
            .first()
        )
        if plan_feature is not None and not plan_feature.is_enabled:
            return False

        return True


class TenantFeatureOverrideService:
    """Activa/desactiva UNA caracteristica para UN tenant especifico sin
    tocar su plan contratado (Especificacion de API §4.25). Solo SUPER_ADMIN."""

    @staticmethod
    def set_override(
        tenant: Tenant, feature_code: str, is_enabled: bool
    ) -> TenantFeatureOverride:
        override, _ = TenantFeatureOverride.objects.update_or_create(
            tenant=tenant,
            feature_code=feature_code,
            defaults={"is_enabled": is_enabled},
        )
        return override

    @staticmethod
    def remove_override(tenant: Tenant, feature_code: str) -> None:
        TenantFeatureOverride.objects.filter(
            tenant=tenant, feature_code=feature_code
        ).delete()
