"""Tareas de Celery propias de usuarios.

Incluye el envio asincrono de correo transaccional (reseteo de contraseña)
via Celery (TRD §5.4) -la creacion mensual de particiones de audit_logs
vive en inventario/tasks.py junto a la de inventory_movements, porque
ambas comparten el mismo helper y se disparan desde una unica tarea.

Sprint 33: generate_data_export() arma y sube a S3 el respaldo completo del
negocio (Ley N 29733, API Spec §4.17); expire_data_exports() lo borra de S3
pasado su TTL -un respaldo con datos personales de clientes y empleados no
puede quedar indefinidamente en un bucket.
"""

import logging

import boto3
from celery import shared_task
from django.conf import settings
from django.core.mail import send_mail
from django.template.loader import render_to_string
from django.utils import timezone
from django.utils.html import strip_tags
from django_tenants.utils import get_public_schema_name, schema_context

logger = logging.getLogger(__name__)


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


@shared_task
def generate_data_export(*, export_id: int, schema_name: str) -> None:
    with schema_context(schema_name):
        from usuarios.models import DataExport
        from usuarios.services import AuditLogService, TenantDataExportService

        export = DataExport.objects.filter(id=export_id).first()
        if export is None:
            return

        export.status = "PROCESSING"
        export.save(update_fields=["status"])

        try:
            content, extension = TenantDataExportService.build_export_file(
                format=export.format
            )
            key = f"tenant-exports/{schema_name}/{export.id}.{extension}"
            client = boto3.client("s3", region_name=settings.AWS_S3_REGION)
            client.put_object(
                Bucket=settings.AWS_STORAGE_BUCKET_NAME, Key=key, Body=content
            )
        except Exception as exc:
            export.status = "FAILED"
            export.error_message = str(exc)[:255]
            export.save(update_fields=["status", "error_message"])
            logger.exception("Fallo la generacion del respaldo #%s.", export_id)
            raise

        export.file_key = key
        export.status = "COMPLETED"
        export.completed_at = timezone.now()
        export.expires_at = export.completed_at + timezone.timedelta(
            hours=TenantDataExportService.EXPORT_TTL_HOURS
        )
        export.save(update_fields=["file_key", "status", "completed_at", "expires_at"])
        AuditLogService.log_action(
            user=export.requested_by,
            action="DATA_EXPORTED",
            entity="DataExport",
            entity_id=export.id,
        )


@shared_task
def expire_data_exports() -> None:
    """Tarea periodica (Sprint 33): borra de S3 los respaldos vencidos y
    marca la fila como EXPIRED, en todos los tenants."""
    from core.models import Tenant

    for tenant in Tenant.objects.exclude(schema_name=get_public_schema_name()):
        with schema_context(tenant.schema_name):
            _expire_data_exports_in_current_schema()


def _expire_data_exports_in_current_schema() -> None:
    from usuarios.models import DataExport

    expired = DataExport.objects.filter(
        status="COMPLETED", expires_at__lt=timezone.now()
    )
    if not expired.exists():
        return

    client = boto3.client("s3", region_name=settings.AWS_S3_REGION)
    for export in expired:
        try:
            client.delete_object(
                Bucket=settings.AWS_STORAGE_BUCKET_NAME, Key=export.file_key
            )
        except Exception:
            logger.exception("Fallo al borrar de S3 el respaldo #%s.", export.id)
        export.status = "EXPIRED"
        export.save(update_fields=["status"])
