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
