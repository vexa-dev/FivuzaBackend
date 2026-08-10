from django.shortcuts import get_object_or_404
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from core.permissions import RequiresFeature, TenantNotCanceled, TenantNotSuspended
from gimnasio.models import (
    ClassBooking,
    ClassSchedule,
    GymClass,
    Membership,
    MembershipGroup,
    MembershipPlan,
)
from gimnasio.serializers import (
    ClassBookingCreateSerializer,
    ClassBookingSerializer,
    ClassScheduleSerializer,
    GymClassSerializer,
    MembershipCreateSerializer,
    MembershipGroupCreateSerializer,
    MembershipGroupSerializer,
    MembershipPlanSerializer,
    MembershipRenewSerializer,
    MembershipSerializer,
)
from gimnasio.services import ClassBookingService, MembershipService
from usuarios.permissions import HasModulePermission

# Mismo esquema que RRHH (Sprint 22): un solo nivel de permiso para todo el
# modulo, sin split lectura/escritura -el gimnasio es chico, no hay un rol
# "solo lectura" documentado para el que valga la pena separar.
_GYM_PERMISSIONS = [
    IsAuthenticated,
    TenantNotSuspended,
    TenantNotCanceled,
    RequiresFeature("HAS_GYM_MODULE"),
    HasModulePermission("GYM_MANAGE"),
]


class MembershipPlanViewSet(viewsets.ModelViewSet):
    queryset = MembershipPlan.objects.all().order_by("name")
    serializer_class = MembershipPlanSerializer
    permission_classes = _GYM_PERMISSIONS

    def get_queryset(self):
        queryset = super().get_queryset()
        if self.request.query_params.get("active_only") == "true":
            queryset = queryset.filter(is_active=True)
        return queryset


class MembershipViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    viewsets.GenericViewSet,
):
    """Sin update/destroy: el ciclo de vida de una membresia avanza solo via
    las acciones dedicadas (renew/freeze/unfreeze/cancel), nunca editando
    sus fechas a mano (mismo criterio que ProductReservation)."""

    queryset = Membership.objects.all().order_by("-created_at")
    serializer_class = MembershipSerializer
    permission_classes = _GYM_PERMISSIONS

    def get_serializer_class(self):
        if self.action == "create":
            return MembershipCreateSerializer
        return MembershipSerializer

    def get_queryset(self):
        queryset = super().get_queryset().prefetch_related("payments")
        params = self.request.query_params
        customer_id = params.get("customer")
        if customer_id:
            queryset = queryset.filter(customer_id=customer_id)
        status_filter = params.get("status")
        if status_filter:
            queryset = queryset.filter(status=status_filter)
        return queryset

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        membership = serializer.save()
        return Response(
            MembershipSerializer(membership).data, status=status.HTTP_201_CREATED
        )

    @action(detail=True, methods=["post"], url_path="renew")
    def renew(self, request, pk=None):
        membership = get_object_or_404(self.get_queryset(), pk=pk)
        serializer = MembershipRenewSerializer(
            data=request.data, context={"membership": membership, "request": request}
        )
        serializer.is_valid(raise_exception=True)
        membership = serializer.save()
        # get_queryset() trae "payments" con prefetch_related -si la renovacion
        # registro un pago nuevo, ese cache prefetcheado en `membership` sigue
        # apuntando a los pagos de ANTES de crearlo. Se vuelve a pedir el
        # objeto para que la respuesta incluya el pago recien creado.
        membership = self.get_queryset().get(pk=membership.pk)
        return Response(MembershipSerializer(membership).data)

    @action(detail=True, methods=["post"], url_path="freeze")
    def freeze(self, request, pk=None):
        membership = get_object_or_404(self.get_queryset(), pk=pk)
        membership = MembershipService.freeze_membership(
            membership=membership, user=request.user
        )
        return Response(MembershipSerializer(membership).data)

    @action(detail=True, methods=["post"], url_path="unfreeze")
    def unfreeze(self, request, pk=None):
        membership = get_object_or_404(self.get_queryset(), pk=pk)
        membership = MembershipService.unfreeze_membership(
            membership=membership, user=request.user
        )
        return Response(MembershipSerializer(membership).data)

    @action(detail=True, methods=["post"], url_path="cancel")
    def cancel(self, request, pk=None):
        membership = get_object_or_404(self.get_queryset(), pk=pk)
        membership = MembershipService.cancel_membership(
            membership=membership, user=request.user
        )
        return Response(MembershipSerializer(membership).data)


class GymClassViewSet(viewsets.ModelViewSet):
    queryset = GymClass.objects.select_related("instructor").order_by("name")
    serializer_class = GymClassSerializer
    permission_classes = _GYM_PERMISSIONS

    def get_queryset(self):
        queryset = super().get_queryset()
        if self.request.query_params.get("active_only") == "true":
            queryset = queryset.filter(is_active=True)
        return queryset


class ClassScheduleViewSet(viewsets.ModelViewSet):
    queryset = ClassSchedule.objects.select_related("gym_class").order_by(
        "day_of_week", "start_time"
    )
    serializer_class = ClassScheduleSerializer
    permission_classes = _GYM_PERMISSIONS

    def get_queryset(self):
        queryset = super().get_queryset()
        gym_class_id = self.request.query_params.get("gym_class")
        if gym_class_id:
            queryset = queryset.filter(gym_class_id=gym_class_id)
        return queryset


class ClassBookingViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    viewsets.GenericViewSet,
):
    queryset = ClassBooking.objects.all().order_by("-class_date", "-created_at")
    serializer_class = ClassBookingSerializer
    permission_classes = _GYM_PERMISSIONS

    def get_serializer_class(self):
        if self.action == "create":
            return ClassBookingCreateSerializer
        return ClassBookingSerializer

    def get_queryset(self):
        queryset = super().get_queryset().select_related("customer", "gym_class")
        params = self.request.query_params
        customer_id = params.get("customer")
        if customer_id:
            queryset = queryset.filter(customer_id=customer_id)
        gym_class_id = params.get("gym_class")
        if gym_class_id:
            queryset = queryset.filter(gym_class_id=gym_class_id)
        class_date = params.get("class_date")
        if class_date:
            queryset = queryset.filter(class_date=class_date)
        return queryset

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        booking = serializer.save()
        return Response(
            ClassBookingSerializer(booking).data, status=status.HTTP_201_CREATED
        )

    @action(detail=True, methods=["post"], url_path="attend")
    def attend(self, request, pk=None):
        booking = get_object_or_404(self.get_queryset(), pk=pk)
        attended = bool(request.data.get("attended", True))
        booking = ClassBookingService.mark_attendance(
            booking=booking, attended=attended
        )
        return Response(ClassBookingSerializer(booking).data)

    @action(detail=True, methods=["post"], url_path="cancel")
    def cancel(self, request, pk=None):
        booking = get_object_or_404(self.get_queryset(), pk=pk)
        booking = ClassBookingService.cancel_booking(booking=booking)
        return Response(ClassBookingSerializer(booking).data)


class MembershipGroupViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    viewsets.GenericViewSet,
):
    queryset = (
        MembershipGroup.objects.all()
        .prefetch_related("memberships")
        .order_by("-created_at")
    )
    serializer_class = MembershipGroupSerializer
    permission_classes = _GYM_PERMISSIONS

    def get_serializer_class(self):
        if self.action == "create":
            return MembershipGroupCreateSerializer
        return MembershipGroupSerializer

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        group = serializer.save()
        group = self.get_queryset().get(pk=group.pk)
        return Response(
            MembershipGroupSerializer(group).data, status=status.HTTP_201_CREATED
        )
