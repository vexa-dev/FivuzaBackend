from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response


@api_view(["GET"])
@permission_classes([AllowAny])
def health_check(request):
    """
    Endpoint de monitoreo (Health Check)
    Devuelve 200 OK y un estado "healthy" para indicar que la aplicación está viva y respondiendo.
    """
    return Response({"status": "healthy"})
