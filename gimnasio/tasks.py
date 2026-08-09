"""Tareas de Celery propias de gimnasio."""

import logging

from celery import shared_task
from django.conf import settings
from django.core.mail import send_mail
from django.template.loader import render_to_string
from django.utils.html import strip_tags
from django_tenants.utils import get_tenant_model, schema_context

logger = logging.getLogger(__name__)


@shared_task
def expire_overdue_memberships() -> None:
    """Celery Beat periodica (Sprint 29): una membresia vencida se marca
    EXPIRED automaticamente -mismo patron multi-tenant que ventas.tasks.
    expire_overdue_reservations."""
    from gimnasio.services import MembershipService

    tenant_model = get_tenant_model()
    for tenant in tenant_model.objects.exclude(schema_name="public"):
        with schema_context(tenant.schema_name):
            expired = MembershipService.expire_overdue_memberships()
            if expired:
                logger.info(
                    "Membresias vencidas en %s: %s.", tenant.schema_name, expired
                )


@shared_task
def alert_expiring_memberships() -> None:
    """Celery Beat diaria (Sprint 29, Ficha de Producto §5.1: "el sistema
    avisa antes de que una vencer"). Customer no tiene email en la BDD v5
    -el aviso llega a los administradores del tenant con el listado de
    socios por vencer, mismo patron que ventas.tasks.
    send_cash_difference_alert (schema_context + admins + render_to_string
    + send_mail), no directo al socio."""
    from gimnasio.services import MembershipService
    from usuarios.models import User

    tenant_model = get_tenant_model()
    for tenant in tenant_model.objects.exclude(schema_name="public"):
        with schema_context(tenant.schema_name):
            expiring = list(MembershipService.get_expiring_soon(days=7))
            if not expiring:
                continue

            recipients = list(
                User.objects.filter(role__name="admin", is_active=True).values_list(
                    "email", flat=True
                )
            )
            if not recipients:
                continue

            html_body = render_to_string(
                "gimnasio/emails/expiring_memberships.html",
                {
                    "memberships": [
                        {
                            "customer_name": m.customer.name,
                            "plan_name": m.plan.name,
                            "end_date": m.end_date,
                        }
                        for m in expiring
                    ]
                },
            )
            send_mail(
                subject=f"Membresías por vencer en los próximos 7 días ({len(expiring)})",
                message=strip_tags(html_body),
                html_message=html_body,
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=recipients,
            )
