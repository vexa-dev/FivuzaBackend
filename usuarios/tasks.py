"""Tareas de Celery propias de usuarios.

Incluye el envio asincrono de correo transaccional (reseteo de contraseña)
via Celery (TRD §5.4) -la creacion mensual de particiones de audit_logs
vive en inventario/tasks.py junto a la de inventory_movements, porque
ambas comparten el mismo helper y se disparan desde una unica tarea.
"""

from celery import shared_task
from django.conf import settings
from django.core.mail import send_mail
from django.template.loader import render_to_string
from django.utils.html import strip_tags
from django_tenants.utils import schema_context


@shared_task
def send_password_reset_email(
    *, user_id: int, token: str, schema_name: str, frontend_origin: str
) -> None:
    with schema_context(schema_name):
        from usuarios.models import User

        user = User.objects.filter(id=user_id).first()
        if not user:
            return

        reset_url = f"{frontend_origin}/reset-password?token={token}"
        html_body = render_to_string(
            "usuarios/emails/password_reset.html", {"reset_url": reset_url}
        )
        send_mail(
            subject="Recupera tu contraseña de Fivuza",
            message=strip_tags(html_body),
            html_message=html_body,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[user.email],
        )
