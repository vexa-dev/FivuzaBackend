"""Tareas de Celery propias de ventas: notificación al administrador cuando
una CashSession cierra con difference != 0.

send_cash_difference_alert: disparada por CashSessionService.close_session()
cuando abs(difference) supera TenantSettings.cash_difference_alert_threshold
(TRD §5.4). Sigue el mismo patron que core.tasks._send_expiration_warning:
schema_context + admins del tenant + render_to_string + send_mail.
"""

import logging

from celery import shared_task
from django.conf import settings
from django.core.mail import send_mail
from django.template.loader import render_to_string
from django.utils.html import strip_tags
from django_tenants.utils import schema_context

logger = logging.getLogger(__name__)


@shared_task
def send_cash_difference_alert(schema_name: str, session_id: int) -> None:
    with schema_context(schema_name):
        from usuarios.models import User
        from ventas.models import CashSession

        session = (
            CashSession.objects.select_related("cash_register")
            .filter(id=session_id)
            .first()
        )
        if session is None or session.difference is None:
            return

        recipients = list(
            User.objects.filter(role__name="admin", is_active=True).values_list(
                "email", flat=True
            )
        )
        if not recipients:
            return

        html_body = render_to_string(
            "ventas/emails/cash_difference_alert.html",
            {
                "cash_register_name": session.cash_register.name,
                "session_id": session.id,
                "expected_closing_amount": session.expected_closing_amount,
                "counted_closing_amount": session.counted_closing_amount,
                "difference": session.difference,
            },
        )
        send_mail(
            subject=f"Diferencia de arqueo en {session.cash_register.name}",
            message=strip_tags(html_body),
            html_message=html_body,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=recipients,
        )
