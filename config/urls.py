"""
URL configuration for config project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/6.0/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""

from django.contrib import admin
from django.urls import include, path
from drf_spectacular.views import (
    SpectacularAPIView,
    SpectacularRedocView,
    SpectacularSwaggerView,
)
from rest_framework.permissions import IsAuthenticated

from core import views as core_views
from core.permissions import IsPlatformStaff

# La documentacion de la API expone el mapa completo de endpoints y schemas
# del sistema -en produccion no debe quedar publica, solo accesible para el
# equipo interno de Fivuza (platform_staff), igual que el resto del panel core.
_DOCS_PERMISSIONS = {"permission_classes": [IsAuthenticated, IsPlatformStaff]}

urlpatterns = [
    path("admin/", admin.site.urls),
    # OpenAPI Schema
    path(
        "api/schema/",
        SpectacularAPIView.as_view(**_DOCS_PERMISSIONS),
        name="schema",
    ),
    # Swagger UI
    path(
        "api/docs/",
        SpectacularSwaggerView.as_view(url_name="schema", **_DOCS_PERMISSIONS),
        name="swagger-ui",
    ),
    # ReDoc
    path(
        "api/redoc/",
        SpectacularRedocView.as_view(url_name="schema", **_DOCS_PERMISSIONS),
        name="redoc",
    ),
    # Health Check
    path("api/v1/health/", core_views.health_check, name="health_check"),
    path("api/v1/", include("core.urls")),
    path("api/v1/", include("usuarios.urls")),
    path("api/v1/", include("inventario.urls")),
    path("api/v1/", include("ventas.urls")),
    path("api/v1/", include("dashboard.urls")),
    path("api/v1/", include("gimnasio.urls")),
]
