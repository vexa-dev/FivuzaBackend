from django.core.management.base import BaseCommand
from django.db import transaction

from core.models import Plan, PlanFeature

# PRD S6.2 (Alcance del Lanzamiento Comercial): tabla de los 4 planes
# comerciales con sus precios. max_users para PLAN_1/PLAN_2 esta explicito en
# la documentacion ("limitados a 1 usuario" - BDD v5, tabla public.plans). La
# documentacion NO fija un numero exacto de usuarios para PLAN_3/PLAN_4 (solo
# dice "+ Usuarios" en el nombre comercial); se asume 5 y 10 como valores de
# partida razonables para el piloto - a confirmar con el equipo de producto
# antes del lanzamiento comercial real (fines de 2026).
PLANS = [
    {
        "code": "PLAN_1",
        "name": "Plan 1 - Inventario Solo",
        "max_users": 1,
        "price_monthly": "29.00",
        "price_semiannual": "145.00",
        "price_annual": "290.00",
        # HAS_SALES_MODULE=False explicito, no solo omitido -Sprint 35
        # confirmo que FeatureFlagService.is_enabled() por diseno defaultea
        # a habilitado cuando no encuentra una fila de PlanFeature (el mismo
        # patron "solo puede apagar" que TenantSettings), asi que omitir la
        # feature nunca la bloquea. Sin esta fila, "Inventario Solo" no
        # bloqueaba Ventas en absoluto.
        "features": {
            "HAS_VARIANTS": True,
            "HAS_MULTI_WAREHOUSE": True,
            "HAS_SALES_MODULE": False,
        },
    },
    {
        "code": "PLAN_2",
        "name": "Plan 2 - Ventas + Inventario",
        "max_users": 1,
        "price_monthly": "39.00",
        "price_semiannual": "195.00",
        "price_annual": "390.00",
        "features": {
            "HAS_VARIANTS": True,
            "HAS_MULTI_WAREHOUSE": True,
            "HAS_SALES_MODULE": True,
        },
    },
    {
        "code": "PLAN_3",
        "name": "Plan 3 - Inventario + Usuarios",
        "max_users": 5,
        "price_monthly": "49.00",
        "price_semiannual": "245.00",
        "price_annual": "490.00",
        # Mismo motivo que PLAN_1: "Inventario + Usuarios" tampoco incluye Ventas.
        "features": {
            "HAS_VARIANTS": True,
            "HAS_MULTI_WAREHOUSE": True,
            "HAS_SALES_MODULE": False,
        },
    },
    {
        "code": "PLAN_4",
        "name": "Plan 4 - Ventas + Inventario + Usuarios",
        "max_users": 10,
        "price_monthly": "59.00",
        "price_semiannual": "295.00",
        "price_annual": "590.00",
        "features": {
            "HAS_VARIANTS": True,
            "HAS_MULTI_WAREHOUSE": True,
            "HAS_SALES_MODULE": True,
        },
    },
]


class Command(BaseCommand):
    help = "Siembra los 4 planes comerciales (PRD S6.2) y sus plan_features. Idempotente: actualiza si ya existen."

    @transaction.atomic
    def handle(self, *args, **options):
        for plan_data in PLANS:
            features = plan_data.pop("features")
            plan, created = Plan.objects.update_or_create(
                code=plan_data["code"], defaults=plan_data
            )
            plan_data["features"] = features
            for feature_code, is_enabled in features.items():
                PlanFeature.objects.update_or_create(
                    plan=plan,
                    feature_code=feature_code,
                    defaults={"is_enabled": is_enabled},
                )
            verb = "Creado" if created else "Actualizado"
            self.stdout.write(self.style.SUCCESS(f"{verb}: {plan.code} - {plan.name}"))
