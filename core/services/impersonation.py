from datetime import timedelta

from django.utils import timezone
from rest_framework.exceptions import APIException

from core.models import (
    PlatformStaff,
    Tenant,
    TenantImpersonationSession,
)
from core.services.platform import PlatformAuditLogService
from core.services.tenants import IMPERSONATION_SESSION_MINUTES


class NoImpersonableAdminUserError(APIException):
    status_code = 400
    default_code = "NO_ADMIN_USER"
    default_detail = {
        "error": {
            "code": "NO_ADMIN_USER",
            "message": "Este tenant no tiene un usuario administrador activo para impersonar.",
        }
    }


class TenantImpersonationService:
    """Sesion de soporte tecnico (Especificacion de API §4.24; Ficha de
    Producto §6): platform_staff (SUPER_ADMIN/SUPPORT) actua temporalmente
    como el admin del propio tenant, sin pedirle su contraseña. Expira sola
    a los IMPERSONATION_SESSION_MINUTES, o antes si se termina explicitamente
    -TenantValidatedJWTAuthentication rechaza el token apenas ended_at deja
    de ser null, asi que "terminar de inmediato" es real, no solo cosmetico."""

    @staticmethod
    def start_impersonation(staff: PlatformStaff, tenant: Tenant, reason: str) -> dict:
        from rest_framework_simplejwt.settings import api_settings
        from rest_framework_simplejwt.tokens import AccessToken

        admin, permission_codes = TenantImpersonationService._find_admin_user(tenant)

        expires_at = timezone.now() + timedelta(minutes=IMPERSONATION_SESSION_MINUTES)
        session = TenantImpersonationSession.objects.create(
            tenant=tenant, platform_staff=staff, reason=reason, expires_at=expires_at
        )

        access = AccessToken()
        access[api_settings.USER_ID_CLAIM] = admin.id
        access["schema_name"] = tenant.schema_name
        # impersonated_by_staff_id: lo consume el frontend del ERP para
        # mostrar el banner persistente (API Spec §4.24). impersonation_
        # session_id: lo consume el backend para poder revocar el token
        # antes de su vencimiento natural (ver _validate_impersonation_claim).
        access["impersonated_by_staff_id"] = staff.id
        access["impersonation_session_id"] = session.id
        access.set_exp(lifetime=timedelta(minutes=IMPERSONATION_SESSION_MINUTES))

        PlatformAuditLogService.log_action(
            staff=staff,
            action="TENANT_IMPERSONATION_STARTED",
            entity="Tenant",
            entity_id=tenant.id,
            details={
                "reason": reason,
                "session_id": session.id,
                "impersonated_user": admin.email,
            },
        )
        TenantImpersonationService._log_tenant_side(
            tenant,
            admin.id,
            "SUPPORT_IMPERSONATION_STARTED",
            session,
            f"Sesion de soporte iniciada por {staff.email}. Motivo: {reason}",
        )

        return {
            "session_id": session.id,
            "access_token": str(access),
            "expires_at": expires_at,
            # No forma parte del contrato literal de la Especificacion de API
            # §4.24 (que solo documenta session_id/access_token/expires_at),
            # pero sin esto el frontend del ERP no tiene forma de saber los
            # permisos del admin impersonado para renderizar el panel
            # correctamente -mismo shape que el "user" del login normal.
            "user": {
                "id": admin.id,
                "email": admin.email,
                "role": admin.role.name,
                "permissions": sorted(permission_codes),
            },
        }

    @staticmethod
    def end_session(session: TenantImpersonationSession) -> None:
        session.ended_at = timezone.now()
        session.save(update_fields=["ended_at"])

        PlatformAuditLogService.log_action(
            staff=session.platform_staff,
            action="TENANT_IMPERSONATION_ENDED",
            entity="Tenant",
            entity_id=session.tenant_id,
            details={"session_id": session.id},
        )
        TenantImpersonationService._log_tenant_side(
            session.tenant,
            None,
            "SUPPORT_IMPERSONATION_ENDED",
            session,
            "Sesion de soporte finalizada.",
            fallback_admin_lookup=True,
        )

    @staticmethod
    def _find_admin_user(tenant: Tenant):
        from django_tenants.utils import schema_context

        with schema_context(tenant.schema_name):
            from usuarios.models import User
            from usuarios.services import PermissionService

            admin = (
                User.objects.select_related("role")
                .filter(role__name="admin", is_active=True)
                .order_by("id")
                .first()
            )
            if admin is None:
                raise NoImpersonableAdminUserError()
            # select_related ya cacheo admin.role en esta instancia -sin
            # esto, acceder a admin.role.name mas tarde (fuera de este
            # schema_context, de vuelta en el esquema public del request de
            # platform_staff) dispararia un nuevo SELECT contra una tabla
            # "roles" que no existe fuera del esquema del tenant.
            return admin, PermissionService.get_permission_codes(admin)

    @staticmethod
    def _log_tenant_side(
        tenant: Tenant,
        user_id: int | None,
        action: str,
        session: TenantImpersonationSession,
        details: str,
        fallback_admin_lookup: bool = False,
    ) -> None:
        from django_tenants.utils import schema_context

        with schema_context(tenant.schema_name):
            from usuarios.models import User
            from usuarios.services import AuditLogService

            if user_id is None and fallback_admin_lookup:
                user = (
                    User.objects.filter(role__name="admin", is_active=True)
                    .order_by("id")
                    .first()
                )
            else:
                user = User.objects.filter(id=user_id).first()
            if user is None:
                return
            AuditLogService.log_action(
                user=user,
                action=action,
                entity="TenantImpersonationSession",
                entity_id=session.id,
                details=details,
            )
