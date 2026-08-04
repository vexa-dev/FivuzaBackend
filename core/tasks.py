"""Tareas de Celery propias de core.

TenantProvisioningService: aprovisiona un tenant nuevo (roles por defecto,
almacén Principal, caja por defecto, tenant_settings) al recibir la señal
post_save de Tenant.

provision_tenant_async: la parte pesada del aprovisionamiento (sembrar roles
dentro del esquema ya migrado) corre en un worker de Celery en vez de bloquear
la respuesta HTTP de POST /core/tenants/register/ (Especificacion de API
§4.9). TenantSettings sigue creandose sincrono en el signal post_save -es una
sola fila, no amerita la complejidad de seguimiento asincrono.

check_subscription_expirations: tarea periodica diaria (Sprint 8, TRD §5.4)
que avisa por correo antes del vencimiento y marca past_due lo ya vencido.
"""

import logging
from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.core.mail import send_mail
from django.template.loader import render_to_string
from django.utils import timezone
from django.utils.html import strip_tags
from django_tenants.utils import get_public_schema_name, schema_context

logger = logging.getLogger(__name__)

# La documentacion (TRD §5.4) pide avisar "N dias antes del vencimiento" sin
# fijar N -se asume 7 dias como valor de partida razonable (una semana de
# margen para que el negocio transfiera el pago), a confirmar con el equipo.
_EXPIRATION_WARNING_DAYS = 7
# Bandeja del equipo interno para el mismo aviso -no hay un campo de "email
# de contacto" en Tenant (BDD v5), asi que el correo del tenant sale de sus
# usuarios con rol "admin" dentro de su propio esquema.
_BILLING_TEAM_EMAIL = "billing@fivuza.com"


@shared_task
def provision_tenant_async(tenant_id: int) -> None:
    from core.models import Tenant
    from core.services import TenantProvisioningService

    tenant = Tenant.objects.filter(id=tenant_id).first()
    if tenant is None:
        return

    tenant.provisioning_status = "IN_PROGRESS"
    tenant.save(update_fields=["provisioning_status"])

    try:
        TenantProvisioningService.seed_default_roles(tenant)
        TenantProvisioningService.seed_default_resources(tenant)
    except Exception:
        tenant.provisioning_status = "FAILED"
        tenant.save(update_fields=["provisioning_status"])
        logger.exception(
            "Aprovisionamiento fallido para tenant %s (%s).",
            tenant.id,
            tenant.schema_name,
        )
        raise

    tenant.provisioning_status = "COMPLETED"
    tenant.save(update_fields=["provisioning_status"])


@shared_task
def check_subscription_expirations() -> None:
    _warn_expiring_subscriptions()
    _mark_expired_subscriptions_past_due()


def _warn_expiring_subscriptions() -> None:
    from core.models import Subscription

    threshold_date = (timezone.now() + timedelta(days=_EXPIRATION_WARNING_DAYS)).date()
    subscriptions = Subscription.objects.filter(
        status="active", expires_at__date=threshold_date
    ).select_related("tenant")

    for subscription in subscriptions:
        _send_expiration_warning(subscription)


def _send_expiration_warning(subscription) -> None:
    tenant = subscription.tenant
    html_body = render_to_string(
        "core/emails/subscription_expiring.html",
        {
            "company_name": tenant.company_name,
            "expires_at": subscription.expires_at.strftime("%d/%m/%Y"),
        },
    )
    recipients = _tenant_admin_emails(tenant) + [_BILLING_TEAM_EMAIL]
    send_mail(
        subject=f"Tu suscripcion a Fivuza vence el {subscription.expires_at:%d/%m/%Y}",
        message=strip_tags(html_body),
        html_message=html_body,
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=recipients,
    )


def _tenant_admin_emails(tenant) -> list[str]:
    if tenant.schema_name == get_public_schema_name():
        return []

    with schema_context(tenant.schema_name):
        from usuarios.models import User

        return list(
            User.objects.filter(role__name="admin", is_active=True).values_list(
                "email", flat=True
            )
        )


def _mark_expired_subscriptions_past_due() -> None:
    from core.models import Subscription

    Subscription.objects.filter(status="active", expires_at__lt=timezone.now()).update(
        status="past_due"
    )
