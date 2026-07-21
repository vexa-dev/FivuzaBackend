import os

import redis
from django.db import connection
from django.db.utils import OperationalError
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response


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
