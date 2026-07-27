from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import RefreshToken

from usuarios.models import (
    AuditLog,
    Permission,
    Role,
    RolePermission,
    RolePermissionsHistory,
    User,
    UserPermission,
)
from usuarios.permissions import HasModulePermission
from usuarios.serializers import (
    AuditLogSerializer,
    PermissionSerializer,
    RolePermissionSerializer,
    RolePermissionsHistorySerializer,
    RoleSerializer,
    TenantUserTokenObtainSerializer,
    UserPermissionSerializer,
    UserSerializer,
)
from usuarios.services import AuditLogService, PermissionService, RoleService


class TenantUserLoginView(APIView):
    """POST email/password de un usuario del tenant -> par de tokens JWT
    (API Spec §3.1). Resuelve el tenant por el subdominio de la request,
    ya procesado por TenantMainMiddleware antes de llegar aqui."""

    permission_classes = [AllowAny]

    def post(self, request):
        serializer = TenantUserTokenObtainSerializer(
            data=request.data, context={"request": request}
        )
        serializer.is_valid(raise_exception=True)
        return Response(serializer.validated_data, status=status.HTTP_200_OK)


class TenantUserLogoutView(APIView):
    """POST refresh token -> lo agrega a la blacklist, invalidandolo."""

    permission_classes = [IsAuthenticated]

    def post(self, request):
        refresh_token = request.data.get("refresh")
        if not refresh_token:
            raise ValidationError({"refresh": "Este campo es requerido."})
        try:
            RefreshToken(refresh_token).blacklist()
        except TokenError as exc:
            raise ValidationError({"refresh": str(exc)})
        return Response(status=status.HTTP_205_RESET_CONTENT)


class RoleViewSet(viewsets.ModelViewSet):
    queryset = Role.objects.all()
    serializer_class = RoleSerializer
    permission_classes = [IsAuthenticated, HasModulePermission("USERS_MANAGE_ROLES")]


class PermissionViewSet(viewsets.ReadOnlyModelViewSet):
    """Catalogo fijo de permisos -no editable via API (API Spec §2.1)."""

    queryset = Permission.objects.all()
    serializer_class = PermissionSerializer
    permission_classes = [IsAuthenticated]


class RolePermissionViewSet(viewsets.ModelViewSet):
    """Asignacion de permisos a un rol. create/destroy pasan siempre por
    RoleService para dejar registro en RolePermissionsHistory (Esquema
    Backend §4.2) -nunca se inserta/borra la fila directamente."""

    queryset = RolePermission.objects.all()
    serializer_class = RolePermissionSerializer
    permission_classes = [IsAuthenticated, HasModulePermission("USERS_MANAGE_ROLES")]

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        role_permission = RoleService.grant_permission(
            role=serializer.validated_data["role"],
            permission=serializer.validated_data["permission"],
            changed_by=request.user,
        )
        AuditLogService.log_action(
            user=request.user,
            action="USER_ROLE_CHANGED",
            entity="Role",
            entity_id=role_permission.role_id,
            details={"granted": role_permission.permission.code},
        )
        output = self.get_serializer(role_permission)
        return Response(output.data, status=status.HTTP_201_CREATED)

    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()
        role_id, permission = instance.role_id, instance.permission
        RoleService.revoke_permission(
            role=instance.role, permission=permission, changed_by=request.user
        )
        AuditLogService.log_action(
            user=request.user,
            action="USER_ROLE_CHANGED",
            entity="Role",
            entity_id=role_id,
            details={"revoked": permission.code},
        )
        return Response(status=status.HTTP_204_NO_CONTENT)


class RolePermissionsHistoryViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = RolePermissionsHistory.objects.all()
    serializer_class = RolePermissionsHistorySerializer
    permission_classes = [IsAuthenticated, HasModulePermission("USERS_VIEW_AUDIT")]


class UserViewSet(viewsets.ModelViewSet):
    queryset = User.objects.all()
    serializer_class = UserSerializer
    permission_classes = [IsAuthenticated, HasModulePermission("USERS_MANAGE")]

    def perform_destroy(self, instance):
        instance.deleted_at = timezone.now()
        instance.deleted_by = self.request.user
        instance.is_active = False
        instance.save(update_fields=["deleted_at", "deleted_by", "is_active"])
        PermissionService.invalidate_user_cache(instance.id)


class UserPermissionViewSet(viewsets.ModelViewSet):
    queryset = UserPermission.objects.all()
    serializer_class = UserPermissionSerializer
    permission_classes = [IsAuthenticated, HasModulePermission("USERS_MANAGE")]

    def perform_create(self, serializer):
        super().perform_create(serializer)
        PermissionService.invalidate_user_cache(serializer.instance.user_id)

    def perform_update(self, serializer):
        super().perform_update(serializer)
        PermissionService.invalidate_user_cache(serializer.instance.user_id)

    def perform_destroy(self, instance):
        user_id = instance.user_id
        super().perform_destroy(instance)
        PermissionService.invalidate_user_cache(user_id)


class AuditLogViewSet(viewsets.ReadOnlyModelViewSet):
    """Solo lectura -se escribe unicamente via AuditLogService.log_action(),
    nunca por POST/PUT directo del cliente (API Spec §2.1)."""

    queryset = AuditLog.objects.all().order_by("-created_at")
    serializer_class = AuditLogSerializer
    permission_classes = [IsAuthenticated, HasModulePermission("USERS_VIEW_AUDIT")]
