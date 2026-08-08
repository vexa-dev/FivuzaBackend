"""
ASGI config for config project.

It exposes the ASGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/6.0/howto/deployment/asgi/
"""

import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

# django_asgi_app se obtiene ANTES de importar cualquier cosa que toque
# modelos de Django (channels.routing, dashboard.routing) -es el orden que
# exige la documentacion de Channels para que el registro de apps ya este
# listo cuando esos imports corran.
django_asgi_app = get_asgi_application()

from channels.routing import ProtocolTypeRouter, URLRouter  # noqa: E402

from dashboard.routing import websocket_urlpatterns  # noqa: E402

# Sprint 24 (TRD §2.5): WebSocket del dashboard convive con el resto de la
# API HTTP en el mismo proceso Daphne -no hay ningun cambio para las rutas
# HTTP existentes, siguen resolviendo por TenantMainMiddleware como siempre.
application = ProtocolTypeRouter(
    {
        "http": django_asgi_app,
        "websocket": URLRouter(websocket_urlpatterns),
    }
)
