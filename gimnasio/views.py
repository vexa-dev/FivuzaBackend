from decimal import Decimal

from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core.permissions import RequiresFeature, TenantNotCanceled, TenantNotSuspended
from gimnasio.models import (
    ClassBooking,
    ClassSchedule,
    GymClass,
    Membership,
    MembershipGroup,
    MembershipPayment,
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
from gimnasio.services import AccessCheckService, ClassBookingService, MembershipService
from usuarios.permissions import HasModulePermission
from usuarios.services import ReportExportService

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

    @action(detail=True, methods=["get"], url_path="access-check")
    def access_check(self, request, pk=None):
        """GET -> {"allowed": bool, "reason": str|None} (Sprint 31, Ficha de
        Producto §5.1). Pensado para ser consultado por cualquier hardware
        de control de acceso de terreno (torniquete, lector QR) sin
        acoplar el backend a una marca especifica."""
        membership = get_object_or_404(self.get_queryset(), pk=pk)
        return Response(AccessCheckService.check_access(membership))

    @action(detail=True, methods=["get"], url_path="qr")
    def qr(self, request, pk=None):
        membership = get_object_or_404(self.get_queryset(), pk=pk)
        content = AccessCheckService.generate_qr_png(membership)
        return HttpResponse(content, content_type="image/png")


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


class CheckInView(APIView):
    """POST /gimnasio/check-in/ {"token": "..."} o {"membership_id": N}
    (Sprint 31, Ficha de Producto §5.1). Resuelve el token del QR (o el id
    tipeado a mano, para la busqueda manual) a una membresia y devuelve el
    resultado de AccessCheckService junto con el nombre del socio y del
    plan, para que la pantalla de check-in muestre algo mas util que un
    simple id."""

    permission_classes = _GYM_PERMISSIONS

    def post(self, request):
        token = request.data.get("token")
        membership_id = request.data.get("membership_id")
        if token:
            membership_id = AccessCheckService.parse_qr_token(token)
        if not membership_id:
            raise ValidationError("token o membership_id son requeridos.")

        membership = get_object_or_404(
            Membership.objects.select_related("customer", "plan"), pk=membership_id
        )
        result = AccessCheckService.check_access(membership)
        return Response(
            {
                **result,
                "membership_id": membership.id,
                "customer_name": membership.customer.name,
                "plan_name": membership.plan.name,
                "end_date": membership.end_date,
            }
        )


class ClassAttendanceReportView(APIView):
    """GET /gimnasio/reports/class-attendance/?date_from=&date_to=&gym_class=&export=
    (Sprint 31). Asistencia y ocupacion por clase+fecha en un rango."""

    permission_classes = _GYM_PERMISSIONS

    def get(self, request):
        date_from = request.query_params.get("date_from")
        date_to = request.query_params.get("date_to")
        if not date_from or not date_to:
            raise ValidationError("date_from y date_to son requeridos.")

        queryset = ClassBooking.objects.select_related("gym_class").filter(
            class_date__gte=date_from, class_date__lte=date_to
        )
        gym_class_id = request.query_params.get("gym_class")
        if gym_class_id:
            queryset = queryset.filter(gym_class_id=gym_class_id)

        summary: dict[tuple[int, str], dict] = {}
        for booking in queryset:
            key = (booking.gym_class_id, str(booking.class_date))
            bucket = summary.setdefault(
                key,
                {
                    "gym_class_id": booking.gym_class_id,
                    "class_name": booking.gym_class.name,
                    "class_date": str(booking.class_date),
                    "max_capacity": booking.gym_class.max_capacity,
                    "reserved_count": 0,
                    "attended_count": 0,
                    "no_show_count": 0,
                    "cancelled_count": 0,
                },
            )
            field = {
                "RESERVADO": "reserved_count",
                "ASISTIO": "attended_count",
                "NO_ASISTIO": "no_show_count",
                "CANCELADO": "cancelled_count",
            }[booking.status]
            bucket[field] += 1

        rows = sorted(
            summary.values(), key=lambda row: (row["class_date"], row["class_name"])
        )
        for row in rows:
            taken = row["reserved_count"] + row["attended_count"]
            row["occupancy_pct"] = (
                round(taken / row["max_capacity"] * 100, 1)
                if row["max_capacity"]
                else 0
            )

        export_format = request.query_params.get("export")
        if export_format:
            columns = [
                "class_name",
                "class_date",
                "max_capacity",
                "reserved_count",
                "attended_count",
                "no_show_count",
                "cancelled_count",
                "occupancy_pct",
            ]
            export_rows = [{key: row[key] for key in columns} for row in rows]
            return ReportExportService.export_queryset(
                rows=export_rows,
                columns=columns,
                format=export_format,
                filename=f"asistencia_clases_{date_from}_a_{date_to}",
            )
        return Response(rows)


class MembershipsExpiringReportView(APIView):
    """GET /gimnasio/reports/memberships-expiring/?days=&export= (Sprint
    31). Reutiliza MembershipService.get_expiring_soon() -misma consulta
    que la tarea de aviso por correo del Sprint 29, expuesta ahora tambien
    como reporte consultable/exportable a demanda."""

    permission_classes = _GYM_PERMISSIONS

    def get(self, request):
        days = int(request.query_params.get("days", 7))
        queryset = MembershipService.get_expiring_soon(days=days)

        rows = [
            {
                "membership_id": membership.id,
                "customer_name": membership.customer.name,
                "plan_name": membership.plan.name,
                "end_date": str(membership.end_date),
            }
            for membership in queryset
        ]

        export_format = request.query_params.get("export")
        if export_format:
            columns = ["customer_name", "plan_name", "end_date"]
            export_rows = [{key: row[key] for key in columns} for row in rows]
            return ReportExportService.export_queryset(
                rows=export_rows,
                columns=columns,
                format=export_format,
                filename=f"membresias_por_vencer_{days}d",
            )
        return Response(rows)


class RevenueByPlanReportView(APIView):
    """GET /gimnasio/reports/revenue-by-plan/?date_from=&date_to=&export=
    (Sprint 31). Ingresos por plan en un rango de fechas, sumando
    MembershipPayment.amount agrupado por MembershipPayment.membership.plan."""

    permission_classes = _GYM_PERMISSIONS

    def get(self, request):
        date_from = request.query_params.get("date_from")
        date_to = request.query_params.get("date_to")
        if not date_from or not date_to:
            raise ValidationError("date_from y date_to son requeridos.")

        queryset = MembershipPayment.objects.select_related("membership__plan").filter(
            created_at__date__gte=date_from, created_at__date__lte=date_to
        )

        summary: dict[int, dict] = {}
        for payment in queryset:
            plan = payment.membership.plan
            bucket = summary.setdefault(
                plan.id,
                {
                    "plan_id": plan.id,
                    "plan_name": plan.name,
                    "total_amount": 0,
                    "payment_count": 0,
                },
            )
            bucket["total_amount"] += payment.amount
            bucket["payment_count"] += 1

        rows = sorted(summary.values(), key=lambda row: row["plan_name"])
        for row in rows:
            row["total_amount"] = str(row["total_amount"].quantize(Decimal("0.01")))

        export_format = request.query_params.get("export")
        if export_format:
            columns = ["plan_name", "payment_count", "total_amount"]
            export_rows = [{key: row[key] for key in columns} for row in rows]
            return ReportExportService.export_queryset(
                rows=export_rows,
                columns=columns,
                format=export_format,
                filename=f"ingresos_por_plan_{date_from}_a_{date_to}",
            )
        return Response(rows)
