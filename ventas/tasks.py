"""Tareas de Celery propias de ventas: notificaciones al administrador
sobre el ciclo de vida de una CashSession.

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

from core.tenant_tasks import run_per_tenant

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


@shared_task
def send_cash_count_submitted_alert(schema_name: str, session_id: int) -> None:
    """Bloque A: el cajero entrego su caja y hay que revisarla. Mismo patron
    que send_cash_difference_alert -sin esto, el cierre en dos pasos
    dependeria de que el administrador se acuerde de mirar el historial."""
    with schema_context(schema_name):
        from usuarios.models import User
        from ventas.models import CashSession

        session = (
            CashSession.objects.select_related("cash_register", "user")
            .filter(id=session_id)
            .first()
        )
        if session is None or session.status != "PENDING_APPROVAL":
            return

        recipients = list(
            User.objects.filter(role__name="admin", is_active=True).values_list(
                "email", flat=True
            )
        )
        if not recipients:
            return

        html_body = render_to_string(
            "ventas/emails/cash_count_submitted_alert.html",
            {
                "cash_register_name": session.cash_register.name,
                "session_id": session.id,
                "counted_by": session.user.email,
                "opening_amount": session.opening_amount,
                "counted_closing_amount": session.counted_closing_amount,
            },
        )
        send_mail(
            subject=f"Caja entregada para revision: {session.cash_register.name}",
            message=strip_tags(html_body),
            html_message=html_body,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=recipients,
        )


@shared_task
def expire_overdue_reservations() -> None:
    """Celery Beat periódica (Sprint 28, Ficha de Producto §5.2): una
    reserva de stock vencida se libera automáticamente sin intervención
    manual -mismo patron multi-tenant que inventario.tasks.
    alert_low_stock_variants (iterar todos los tenants con schema_context)."""
    from ventas.services import ReservationService

    def _run(tenant):
        expired = ReservationService.expire_overdue_reservations()
        if expired:
            logger.info(
                "Reservas vencidas liberadas en %s: %s.",
                tenant.schema_name,
                expired,
            )

    run_per_tenant("expire_overdue_reservations", _run)
