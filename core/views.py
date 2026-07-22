import os

import redis
from django.db import connection
from django.db.utils import OperationalError
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import RefreshToken

from core.models import Tenant
from core.permissions import IsPlatformStaff
from core.serializers import PlatformStaffTokenObtainSerializer
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
