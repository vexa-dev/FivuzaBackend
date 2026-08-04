from django.shortcuts import get_object_or_404
from rest_framework import mixins, status, viewsets
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core.permissions import RequiresFeature, TenantNotCanceled, TenantNotSuspended
from usuarios.permissions import HasModulePermission
from ventas.models import CashMovement, CashRegister, CashSession
from ventas.serializers import (
    CashMovementSerializer,
    CashRegisterSerializer,
    CashSessionCloseSerializer,
    CashSessionOpenSerializer,
    CashSessionSerializer,
)

# Lectura: cualquier tenant.users autenticado con el modulo de caja activo
# (Especificacion de API §2.3: sin permiso de escritura listado = "-" =
# abierto a lectura). Escritura: solo CASH_MANAGE.
_CASH_READ_PERMISSIONS = [
    IsAuthenticated,
    TenantNotSuspended,
    TenantNotCanceled,
    RequiresFeature("HAS_CASH_MODULE"),
]
_CASH_WRITE_PERMISSIONS = [
    IsAuthenticated,
    TenantNotSuspended,
    TenantNotCanceled,
    RequiresFeature("HAS_CASH_MODULE"),
    HasModulePermission("CASH_MANAGE"),
]


class CashRegisterViewSet(viewsets.ModelViewSet):
    queryset = CashRegister.objects.all().order_by("name")
    serializer_class = CashRegisterSerializer

    def get_permissions(self):
        if self.action in ("list", "retrieve"):
            return [permission() for permission in _CASH_READ_PERMISSIONS]
        return [permission() for permission in _CASH_WRITE_PERMISSIONS]


class CashSessionViewSet(
    mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet
):
    """Solo lectura -abrir/cerrar son acciones propias (CashSessionOpenView/
    CashSessionCloseView), no un create/update generico (Especificacion de
    API §4.4)."""

    queryset = CashSession.objects.all().order_by("-opening_at")
    serializer_class = CashSessionSerializer
    permission_classes = _CASH_READ_PERMISSIONS

    def get_queryset(self):
        queryset = super().get_queryset()
        cash_register_id = self.request.query_params.get("cash_register")
        if cash_register_id:
            queryset = queryset.filter(cash_register_id=cash_register_id)
        status_param = self.request.query_params.get("status")
        if status_param:
            queryset = queryset.filter(status=status_param)
        return queryset


class CashMovementViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    viewsets.GenericViewSet,
):
    """Sin update/destroy: un movimiento de caja es un registro de auditoria
    -se corrige con un movimiento de ajuste nuevo, nunca editando/borrando
    el original (mismo principio que PlatformAuditLog/AuditLog)."""

    queryset = CashMovement.objects.all().order_by("-created_at")
    serializer_class = CashMovementSerializer

    def get_permissions(self):
        if self.action in ("list", "retrieve"):
            return [permission() for permission in _CASH_READ_PERMISSIONS]
        return [permission() for permission in _CASH_WRITE_PERMISSIONS]

    def get_queryset(self):
        queryset = super().get_queryset()
        session_id = self.request.query_params.get("cash_session")
        if session_id:
            queryset = queryset.filter(cash_session_id=session_id)
        return queryset


class CashSessionOpenView(APIView):
    """POST -> abre una sesion de caja (Especificacion de API §4.4)."""

    permission_classes = _CASH_WRITE_PERMISSIONS

    def post(self, request):
        serializer = CashSessionOpenSerializer(
            data=request.data, context={"request": request}
        )
        serializer.is_valid(raise_exception=True)
        session = serializer.save()
        return Response(
            CashSessionSerializer(session).data, status=status.HTTP_201_CREATED
        )


class CashSessionCloseView(APIView):
    """POST -> cierra una sesion de caja con arqueo (Especificacion de API
    §4.4): calcula expected_closing_amount y guarda la diferencia contra lo
    contado."""

    permission_classes = _CASH_WRITE_PERMISSIONS

    def post(self, request, pk):
        session = get_object_or_404(CashSession, pk=pk)
        serializer = CashSessionCloseSerializer(
            data=request.data, context={"request": request, "session": session}
        )
        serializer.is_valid(raise_exception=True)
        session = serializer.save()
        return Response(CashSessionSerializer(session).data)
