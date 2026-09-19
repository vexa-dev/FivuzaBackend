"""Middleware propio de core -a diferencia de las 4 apps de negocio, que
reutilizan permission_classes para su logica, esto necesita ejecutarse en
CADA request ya resuelto contra un tenant, sin excepcion."""

import sentry_sdk


class SentryTenantTagMiddleware:
    """Etiqueta cada evento de Sentry con el schema_name del tenant resuelto
    por TenantMainMiddleware (Especificacion de API §4.26; Esquema Backend
    §8.2) -sin este tag, el panel de salud por tenant no tiene forma de
    filtrar los errores de Sentry por negocio. Debe ir DESPUES de
    TenantMainMiddleware en settings.MIDDLEWARE para que request.tenant ya
    este resuelto."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        tenant = getattr(request, "tenant", None)
        if tenant is not None:
            sentry_sdk.set_tag("tenant", tenant.schema_name)
        return self.get_response(request)


class SchemaRouteGuardMiddleware:
    """Separa las rutas del panel interno (esquema public) de las del ERP
    (esquemas de tenant) aunque compartan un solo URLconf -asi el OpenAPI
    sigue describiendo toda la API en un solo schema.

    - En el dominio public solo existen las rutas de plataforma: una ruta de
      negocio (ej. /api/v1/inventario/...) ahi fallaba con 500 porque sus
      tablas no existen en el esquema public.
    - En el dominio de un tenant no se exponen el panel interno ni el admin
      de Django (defensa en profundidad; ademas ya exigen IsPlatformStaff).

    Responde 404 con el contrato de error estandar. Debe ir despues de
    TenantMainMiddleware."""

    _SHARED_PREFIXES = ("/api/v1/health/", "/api/v1/core/legal/", "/static/")
    _PUBLIC_ONLY_PREFIXES = (
        "/api/v1/core/",
        "/api/v1/platform/",
        "/api/schema",
        "/api/docs",
        "/api/redoc",
        "/admin/",
    )

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        from django.http import JsonResponse
        from django_tenants.utils import get_public_schema_name

        tenant = getattr(request, "tenant", None)
        path = request.path
        if tenant is None or path.startswith(self._SHARED_PREFIXES):
            return self.get_response(request)

        is_public = tenant.schema_name == get_public_schema_name()
        is_platform_path = path.startswith(self._PUBLIC_ONLY_PREFIXES)
        if is_public != is_platform_path:
            return JsonResponse(
                {
                    "error": {
                        "code": "NOT_FOUND",
                        "message": "Recurso no encontrado.",
                        "details": {},
                    }
                },
                status=404,
            )
        return self.get_response(request)
