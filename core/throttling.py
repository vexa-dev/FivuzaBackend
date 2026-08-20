import hashlib

from django.db import connection
from rest_framework.permissions import SAFE_METHODS
from rest_framework.throttling import (
    AnonRateThrottle,
    SimpleRateThrottle,
    UserRateThrottle,
)


class LoginRateThrottle(AnonRateThrottle):
    """Sprint 33 (TRD §6.1, §7.2): limita intentos de login por IP en los
    endpoints de autenticacion (tenant.users y platform_staff) -sin esto,
    un endpoint de login sin ningun limite es un vector trivial de fuerza
    bruta contra contraseñas. La tasa vive en
    REST_FRAMEWORK.DEFAULT_THROTTLE_RATES["login"]."""

    scope = "login_ip"


class LoginIdentifierRateThrottle(SimpleRateThrottle):
    scope = "login_identifier"

    def get_cache_key(self, request, view):
        email = str(request.data.get("email", "")).strip().casefold()
        if not email:
            return None
        schema = getattr(connection, "schema_name", "public")
        digest = hashlib.sha256(f"{schema}:{email}".encode()).hexdigest()
        return self.cache_format % {"scope": self.scope, "ident": digest}


class BusinessWriteRateThrottle(UserRateThrottle):
    scope = "business_write"

    def allow_request(self, request, view):
        domain = view.__class__.__module__.partition(".")[0]
        if (
            domain not in {"inventario", "ventas"}
            or request.method in SAFE_METHODS
            or not getattr(request.user, "is_authenticated", False)
        ):
            return True
        return super().allow_request(request, view)
