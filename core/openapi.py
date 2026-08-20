from rest_framework import serializers
from rest_framework.views import APIView


class DynamicObjectSerializer(serializers.Serializer):
    """Objeto JSON dinámico usado por agregados cuyo esquema depende del reporte."""


class SchemaAPIView(APIView):
    """APIView con contrato base explícito para respuestas dinámicas.

    Las vistas con payload de entrada estable deben reemplazar
    ``serializer_class`` por su serializer de dominio. Los reportes que
    devuelven agregados calculados conservan este objeto abierto.
    """

    serializer_class = DynamicObjectSerializer
