import os

import redis
from django.db import connection
from django.db.utils import OperationalError
from django.shortcuts import get_object_or_404
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import api_view, permission_classes
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import RefreshToken

from core.models import (
    PlatformStaff,
    Plan,
    PlanFeature,
    Subscription,
    SubscriptionPayment,
    Tenant,
    TenantSettings,
)
from core.permissions import IsPlatformStaff, require_platform_role
from core.serializers import (
    PlanFeatureSerializer,
    PlanSerializer,
    PlatformStaffCRUDSerializer,
    PlatformStaffTokenObtainSerializer,
    SubscriptionPaymentSerializer,
    SubscriptionSerializer,
    TenantSerializer,
    TenantSettingsSerializer,
)
from core.services import TenantLifecycleService


class PlatformStaffLoginView(APIView):
    """POST email/password de un miembro del equipo Fivuza -> par de tokens JWT."""

    permission_classes = [AllowAny]

    def post(self, request):
        serializer = PlatformStaffTokenObtainSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return Response(serializer.validated_data, status=status.HTTP_200_OK)


class PlatformStaffLogoutView(APIView):
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


class TenantSuspendView(APIView):
    """PATCH -> suspende un tenant (Especificacion de API §4.12). Solo platform_staff."""

    permission_classes = [IsAuthenticated, IsPlatformStaff]

    def patch(self, request, pk):
        tenant = get_object_or_404(Tenant, pk=pk)
        tenant = TenantLifecycleService.suspend_tenant(
            tenant, reason=request.data.get("reason")
        )
        return Response(
            {
                "id": tenant.id,
                "status": tenant.status,
                "suspended_at": tenant.suspended_at,
            }
        )


class TenantReactivateView(APIView):
    """PATCH -> reactiva un tenant (Especificacion de API §4.12). Solo platform_staff."""

    permission_classes = [IsAuthenticated, IsPlatformStaff]

    def patch(self, request, pk):
        tenant = get_object_or_404(Tenant, pk=pk)
        tenant = TenantLifecycleService.reactivate_tenant(tenant)
        return Response({"id": tenant.id, "status": tenant.status})


class TenantViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.UpdateModelMixin,
    mixins.DestroyModelMixin,
    viewsets.GenericViewSet,
):
    """Sin create: el registro de un tenant nuevo es POST /core/tenants/register/
    (Especificacion de API §4.9), un endpoint de accion fuera de este CRUD."""

    queryset = Tenant.objects.all()
    serializer_class = TenantSerializer
    permission_classes = [IsAuthenticated, IsPlatformStaff]


class PlanViewSet(viewsets.ModelViewSet):
    """Lectura publica (sitio de marketing); escritura solo SUPER_ADMIN."""

    queryset = Plan.objects.all()
    serializer_class = PlanSerializer

    def get_permissions(self):
        if self.action in ("list", "retrieve"):
            return [AllowAny()]
        return [IsAuthenticated(), require_platform_role("SUPER_ADMIN")()]


class PlanFeatureViewSet(viewsets.ModelViewSet):
    queryset = PlanFeature.objects.all()
    serializer_class = PlanFeatureSerializer
    permission_classes = [IsAuthenticated, require_platform_role("SUPER_ADMIN")]


class SubscriptionViewSet(viewsets.ModelViewSet):
    queryset = Subscription.objects.all()
    serializer_class = SubscriptionSerializer
    permission_classes = [IsAuthenticated, IsPlatformStaff]


class SubscriptionPaymentViewSet(viewsets.ModelViewSet):
    """Lectura: cualquier platform_staff. Escritura: solo BILLING."""

    queryset = SubscriptionPayment.objects.all()
    serializer_class = SubscriptionPaymentSerializer

    def get_permissions(self):
        if self.action in ("list", "retrieve"):
            return [IsAuthenticated(), IsPlatformStaff()]
        return [IsAuthenticated(), require_platform_role("BILLING")()]


class TenantSettingsViewSet(viewsets.ModelViewSet):
    """Solo platform_staff por ahora. La Especificacion de API tambien permite
    'admin del propio tenant para toggles operativos', pero eso depende de
    PermissionService (usuarios), que llega recien en Sprint 2 -queda
    pendiente para entonces, no se improvisa aqui."""

    queryset = TenantSettings.objects.all()
    serializer_class = TenantSettingsSerializer
    permission_classes = [IsAuthenticated, IsPlatformStaff]


class PlatformStaffViewSet(viewsets.ModelViewSet):
    """Gestion del equipo interno de Fivuza -restringido a SUPER_ADMIN tanto
    para lectura como escritura, dado que expone quien tiene cada rol
    interno (soporte/facturacion/administracion)."""

    queryset = PlatformStaff.objects.all()
    serializer_class = PlatformStaffCRUDSerializer
    permission_classes = [IsAuthenticated, require_platform_role("SUPER_ADMIN")]


@api_view(["GET"])
@permission_classes([AllowAny])
def health_check(request):
    """
    Endpoint de monitoreo (Health Check).
    Verifica conexion real a PostgreSQL y Redis -no solo responde un valor fijo-
    para que un monitor externo (UptimeRobot/CloudWatch) detecte una caida real.
    """
    checks = {"database": _check_database(), "redis": _check_redis()}
    status_code = 200 if all(checks.values()) else 503
    return Response(
        {"status": "healthy" if status_code == 200 else "unhealthy", "checks": checks},
        status=status_code,
    )


def _check_database():
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        return True
    except OperationalError:
        return False


def _check_redis():
    try:
        client = redis.from_url(os.getenv("REDIS_URL", "redis://redis:6379/0"))
        return client.ping()
    except redis.RedisError:
        return False
