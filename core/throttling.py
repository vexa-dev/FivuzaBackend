from rest_framework.throttling import AnonRateThrottle


class LoginRateThrottle(AnonRateThrottle):
    """Sprint 33 (TRD §6.1, §7.2): limita intentos de login por IP en los
    endpoints de autenticacion (tenant.users y platform_staff) -sin esto,
    un endpoint de login sin ningun limite es un vector trivial de fuerza
    bruta contra contraseñas. La tasa vive en
    REST_FRAMEWORK.DEFAULT_THROTTLE_RATES["login"]."""

    scope = "login"
