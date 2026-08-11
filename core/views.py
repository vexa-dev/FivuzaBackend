import os
from datetime import timedelta

import redis
from django.db import connection
from django.db.utils import OperationalError
from django.shortcuts import get_object_or_404
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import api_view, permission_classes
from rest_framework.exceptions import ValidationError
from rest_framework.filters import OrderingFilter
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import RefreshToken

from core.models import (
    PlatformAuditLog,
    PlatformStaff,
    Plan,
    PlanFeature,
    Subscription,
    SubscriptionDiscount,
    SubscriptionPayment,
    Tenant,
    TenantFeatureOverride,
    TenantImpersonationSession,
    TenantSettings,
)
from core.permissions import CanImpersonate, IsPlatformStaff, require_platform_role
from core.throttling import LoginRateThrottle
from core.serializers import (
    PlanFeatureSerializer,
    PlanSerializer,
    PlatformAuditLogSerializer,
    PlatformStaffCRUDSerializer,
    PlatformStaffTokenObtainSerializer,
    SubscriptionDiscountSerializer,
    SubscriptionPaymentSerializer,
    SubscriptionSerializer,
    TenantFeatureOverrideSerializer,
    TenantNoteSerializer,
    TenantRegisterSerializer,
    TenantSerializer,
    TenantSettingsSerializer,
)
from core.services import (
    DATA_RETENTION_GRACE_DAYS,
    PlatformAuditLogService,
    PlatformDashboardService,
    SubscriptionDiscountService,
    SubscriptionPaymentService,
    TenantConsumptionService,
    TenantFeatureOverrideService,
    TenantHealthService,
    TenantImpersonationService,
    TenantLifecycleService,
    TenantNoteService,
    TenantOnboardingService,
)


class AuditLoggedViewSetMixin:
    """Registra automaticamente en platform_audit_logs cada create/update/
    destroy de un ViewSet (Esquema Backend §8.2: "cada vista que ejecuta una
    accion relevante llama explicitamente a este helper"). El nombre de la
    entidad es el nombre del modelo, ej. "Plan", "Subscription"."""

    def perform_create(self, serializer):
        super().perform_create(serializer)
        PlatformAuditLogService.log_action(
            staff=self.request.user,
            action="CREATE",
            entity=serializer.instance.__class__.__name__,
            entity_id=serializer.instance.pk,
        )

    def perform_update(self, serializer):
        super().perform_update(serializer)
        PlatformAuditLogService.log_action(
            staff=self.request.user,
            action="UPDATE",
            entity=serializer.instance.__class__.__name__,
            entity_id=serializer.instance.pk,
        )

    def perform_destroy(self, instance):
        entity = instance.__class__.__name__
        entity_id = instance.pk
        super().perform_destroy(instance)
        PlatformAuditLogService.log_action(
            staff=self.request.user,
            action="DELETE",
            entity=entity,
            entity_id=entity_id,
        )


class PlatformStaffLoginView(APIView):
    """POST email/password de un miembro del equipo Fivuza -> par de tokens JWT."""

    permission_classes = [AllowAny]
    throttle_classes = [LoginRateThrottle]

    def post(self, request):
        serializer = PlatformStaffTokenObtainSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return Response(serializer.validated_data, status=status.HTTP_200_OK)


class PlatformStaffLogoutView(APIView):
    """POST refresh token -> lo agrega a la blacklist, invalidandolo."""

    permission_classes = [IsAuthenticated]

    def post(self, request):
        refresh_token = request.data.get("refresh")
        if not refresh_token:
            raise ValidationError({"refresh": "Este campo es requerido."})
        try:
            RefreshToken(refresh_token).blacklist()
        except TokenError as exc:
            raise ValidationError({"refresh": str(exc)})
        return Response(status=status.HTTP_205_RESET_CONTENT)


class LegalDocumentView(APIView):
    """GET /core/legal/terms/ o /core/legal/privacy/ (Sprint 33, Ley N
    29733) -sin autenticacion a proposito: cualquiera debe poder leer el
    texto vigente antes de aceptarlo."""

    permission_classes = [AllowAny]

    def get(self, request, document):
        from core.legal import get_legal_document

        try:
            return Response(get_legal_document(document))
        except ValueError:
            from django.http import Http404

            raise Http404


class TenantRegisterView(APIView):
    """POST -> registra un tenant nuevo (Especificacion de API §4.9). Solo
    platform_staff -no es un formulario de auto-registro publico (Especificacion
    de API §2.5: la creacion de tenants es "Solo platform_staff")."""

    permission_classes = [IsAuthenticated, IsPlatformStaff]

    def post(self, request):
        serializer = TenantRegisterSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        tenant = serializer.save()
        PlatformAuditLogService.log_action(
            staff=request.user,
            action="REGISTER_TENANT",
            entity="Tenant",
            entity_id=tenant.id,
            details={"company_name": tenant.company_name},
        )
        # provisioning_status ya puede reflejar COMPLETED aqui mismo en tests
        # (CELERY_TASK_ALWAYS_EAGER=True corre la tarea en el mismo proceso);
        # en produccion el worker todavia no la tomo, y sigue en PENDING.
        tenant.refresh_from_db(fields=["provisioning_status"])
        return Response(
            {
                "id": tenant.id,
                "status": tenant.status,
                "provisioning_status": tenant.provisioning_status,
            },
            status=status.HTTP_202_ACCEPTED,
        )


class TenantSuspendView(APIView):
    """PATCH -> suspende un tenant (Especificacion de API §4.12). Solo platform_staff."""

    permission_classes = [IsAuthenticated, IsPlatformStaff]

    def patch(self, request, pk):
        tenant = get_object_or_404(Tenant, pk=pk)
        tenant = TenantLifecycleService.suspend_tenant(
            tenant, reason=request.data.get("reason")
        )
        PlatformAuditLogService.log_action(
            staff=request.user,
            action="SUSPEND_TENANT",
            entity="Tenant",
            entity_id=tenant.id,
            details={"reason": request.data.get("reason")},
        )
        return Response(
            {
                "id": tenant.id,
                "status": tenant.status,
                "suspended_at": tenant.suspended_at,
            }
        )


class TenantReactivateView(APIView):
    """PATCH -> reactiva un tenant (Especificacion de API §4.12). Solo platform_staff."""

    permission_classes = [IsAuthenticated, IsPlatformStaff]

    def patch(self, request, pk):
        tenant = get_object_or_404(Tenant, pk=pk)
        tenant = TenantLifecycleService.reactivate_tenant(tenant)
        PlatformAuditLogService.log_action(
            staff=request.user,
            action="REACTIVATE_TENANT",
            entity="Tenant",
            entity_id=tenant.id,
        )
        return Response({"id": tenant.id, "status": tenant.status})


class TenantCancelView(APIView):
    """PATCH -> cancela un tenant de forma definitiva (Especificacion de API
    §4.12). Solo platform_staff. Transicion sin retorno: un tenant canceled
    no puede reactivarse."""

    permission_classes = [IsAuthenticated, IsPlatformStaff]

    def patch(self, request, pk):
        tenant = get_object_or_404(Tenant, pk=pk)
        tenant = TenantLifecycleService.cancel_tenant(
            tenant, reason=request.data.get("reason")
        )
        PlatformAuditLogService.log_action(
            staff=request.user,
            action="TENANT_CANCELED",
            entity="Tenant",
            entity_id=tenant.id,
            details={"reason": request.data.get("reason")},
        )
        return Response(
            {
                "id": tenant.id,
                "status": tenant.status,
                "canceled_at": tenant.canceled_at,
                "data_retention_until": tenant.canceled_at
                + timedelta(days=DATA_RETENTION_GRACE_DAYS),
            }
        )


class TenantImpersonationStartView(APIView):
    """POST -> inicia una sesion de soporte tecnico (Especificacion de API
    §4.24). Solo SUPER_ADMIN/SUPPORT."""

    permission_classes = [IsAuthenticated, CanImpersonate]

    def post(self, request, pk):
        tenant = get_object_or_404(Tenant, pk=pk)
        reason = request.data.get("reason", "")
        result = TenantImpersonationService.start_impersonation(
            request.user, tenant, reason
        )
        return Response(result, status=status.HTTP_201_CREATED)


class TenantImpersonationEndView(APIView):
    """DELETE -> termina una sesion de soporte antes de que expire sola
    (Especificacion de API §4.24). Solo SUPER_ADMIN/SUPPORT -llamado desde
    el panel core. El botón "Salir" del banner en el ERP del tenant usa
    ImpersonationSelfEndView en su lugar, porque ese contexto esta
    autenticado con el token de tenant.users, no con el de platform_staff."""

    permission_classes = [IsAuthenticated, CanImpersonate]

    def delete(self, request, pk, session_id):
        session = get_object_or_404(
            TenantImpersonationSession,
            id=session_id,
            tenant_id=pk,
            ended_at__isnull=True,
        )
        TenantImpersonationService.end_session(session)
        return Response(status=status.HTTP_204_NO_CONTENT)


class ImpersonationSelfEndView(APIView):
    """POST -> el propio usuario impersonado termina la sesion desde el
    banner del ERP ("Salir"). Se identifica la sesion por el claim
    impersonation_session_id del token que autentico este request -no por
    parametro, para que nadie pueda terminar la sesion de otro tenant."""

    permission_classes = [IsAuthenticated]

    def post(self, request):
        session_id = (
            request.auth.get("impersonation_session_id") if request.auth else None
        )
        if session_id is None:
            raise ValidationError("Esta sesion no es una sesion de soporte activa.")
        session = get_object_or_404(
            TenantImpersonationSession, id=session_id, ended_at__isnull=True
        )
        TenantImpersonationService.end_session(session)
        return Response(status=status.HTTP_205_RESET_CONTENT)


class TenantFeatureOverrideListView(APIView):
    """GET -> caracteristicas con override individual para este tenant
    (Especificacion de API §4.25). Cualquier platform_staff puede leer; solo
    SUPER_ADMIN puede escribir (ver TenantFeatureOverrideView)."""

    permission_classes = [IsAuthenticated, IsPlatformStaff]

    def get(self, request, pk):
        tenant = get_object_or_404(Tenant, pk=pk)
        overrides = TenantFeatureOverride.objects.filter(tenant=tenant)
        return Response(TenantFeatureOverrideSerializer(overrides, many=True).data)


class TenantFeatureOverrideView(APIView):
    """PATCH/DELETE -> activa, desactiva o retira el override de UNA
    caracteristica para ESTE tenant (Especificacion de API §4.25). Solo
    SUPER_ADMIN."""

    permission_classes = [IsAuthenticated, require_platform_role("SUPER_ADMIN")]

    def patch(self, request, pk, feature_code):
        tenant = get_object_or_404(Tenant, pk=pk)
        is_enabled = request.data.get("is_enabled")
        if not isinstance(is_enabled, bool):
            raise ValidationError({"is_enabled": "Este campo es requerido (booleano)."})

        override = TenantFeatureOverrideService.set_override(
            tenant, feature_code, is_enabled
        )
        PlatformAuditLogService.log_action(
            staff=request.user,
            action="TENANT_FEATURE_OVERRIDE_SET",
            entity="Tenant",
            entity_id=tenant.id,
            details={"feature_code": feature_code, "is_enabled": is_enabled},
        )
        return Response(
            {
                "tenant_id": tenant.id,
                "feature_code": override.feature_code,
                "is_enabled": override.is_enabled,
            }
        )

    def delete(self, request, pk, feature_code):
        tenant = get_object_or_404(Tenant, pk=pk)
        TenantFeatureOverrideService.remove_override(tenant, feature_code)
        PlatformAuditLogService.log_action(
            staff=request.user,
            action="TENANT_FEATURE_OVERRIDE_REMOVED",
            entity="Tenant",
            entity_id=tenant.id,
            details={"feature_code": feature_code},
        )
        return Response(status=status.HTTP_204_NO_CONTENT)


class TenantNoteListCreateView(APIView):
    """GET/POST -> notas internas del equipo sobre un tenant (Especificacion
    de API §4.25). Nunca visibles para el propio negocio -viven enteramente
    en core, ningun endpoint de las 4 apps de negocio las expone. Cualquier
    platform_staff puede leer y escribir."""

    permission_classes = [IsAuthenticated, IsPlatformStaff]

    def get(self, request, pk):
        tenant = get_object_or_404(Tenant, pk=pk)
        notes = tenant.notes.select_related("platform_staff")
        return Response(TenantNoteSerializer(notes, many=True).data)

    def post(self, request, pk):
        tenant = get_object_or_404(Tenant, pk=pk)
        text = request.data.get("text", "").strip()
        if not text:
            raise ValidationError({"text": "Este campo es requerido."})
        note = TenantNoteService.add_note(tenant, request.user, text)
        return Response(TenantNoteSerializer(note).data, status=status.HTTP_201_CREATED)


class SubscriptionDiscountListCreateView(APIView):
    """GET/POST -> descuento de suscripcion negociado con un tenant puntual
    (Especificacion de API §4.25). Solo SUPER_ADMIN/BILLING."""

    permission_classes = [
        IsAuthenticated,
        require_platform_role("SUPER_ADMIN", "BILLING"),
    ]

    def get(self, request):
        queryset = SubscriptionDiscount.objects.all()
        subscription_id = request.query_params.get("subscription")
        if subscription_id:
            queryset = queryset.filter(subscription_id=subscription_id)
        return Response(SubscriptionDiscountSerializer(queryset, many=True).data)

    def post(self, request):
        subscription = get_object_or_404(
            Subscription, pk=request.data.get("subscription_id")
        )
        discount = SubscriptionDiscountService.create_discount(
            subscription=subscription,
            discount_percent=request.data.get("discount_percent"),
            override_price=request.data.get("override_price"),
            reason=request.data.get("reason", ""),
            expires_at=request.data.get("expires_at"),
        )
        PlatformAuditLogService.log_action(
            staff=request.user,
            action="SUBSCRIPTION_DISCOUNT_CREATED",
            entity="Subscription",
            entity_id=subscription.id,
            details={
                "discount_percent": request.data.get("discount_percent"),
                "override_price": request.data.get("override_price"),
                "reason": request.data.get("reason", ""),
            },
        )
        return Response(
            SubscriptionDiscountSerializer(discount).data,
            status=status.HTTP_201_CREATED,
        )


class SubscriptionDiscountDetailView(APIView):
    """DELETE -> quita un descuento de suscripcion (Especificacion de API
    §4.25, seccion Frontend: "aplicar/quitar un descuento"). Solo
    SUPER_ADMIN/BILLING."""

    permission_classes = [
        IsAuthenticated,
        require_platform_role("SUPER_ADMIN", "BILLING"),
    ]

    def delete(self, request, pk):
        discount = get_object_or_404(SubscriptionDiscount, pk=pk)
        subscription_id = discount.subscription_id
        SubscriptionDiscountService.remove_discount(discount)
        PlatformAuditLogService.log_action(
            staff=request.user,
            action="SUBSCRIPTION_DISCOUNT_REMOVED",
            entity="Subscription",
            entity_id=subscription_id,
        )
        return Response(status=status.HTTP_204_NO_CONTENT)


class TenantOnboardingView(APIView):
    """GET -> checklist de onboarding computado (Especificacion de API
    §4.26). Solo lectura, cualquier platform_staff."""

    permission_classes = [IsAuthenticated, IsPlatformStaff]

    def get(self, request, pk):
        tenant = get_object_or_404(Tenant, pk=pk)
        return Response(TenantOnboardingService.get_checklist(tenant))


class TenantHealthView(APIView):
    """GET -> panel de salud tecnica por tenant (Especificacion de API
    §4.26). Solo lectura, cualquier platform_staff."""

    permission_classes = [IsAuthenticated, IsPlatformStaff]

    def get(self, request, pk):
        tenant = get_object_or_404(Tenant, pk=pk)
        return Response(TenantHealthService.get_health(tenant))


class TenantConsumptionView(APIView):
    """GET -> reporte de consumo por tenant (Especificacion de API §4.26).
    Solo lectura, cualquier platform_staff."""

    permission_classes = [IsAuthenticated, IsPlatformStaff]

    def get(self, request, pk):
        tenant = get_object_or_404(Tenant, pk=pk)
        return Response(TenantConsumptionService.get_report(tenant))


class TenantViewSet(
    AuditLoggedViewSetMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.UpdateModelMixin,
    mixins.DestroyModelMixin,
    viewsets.GenericViewSet,
):
    """Sin create: el registro de un tenant nuevo es POST /core/tenants/register/
    (Especificacion de API §4.9), un endpoint de accion fuera de este CRUD."""

    queryset = Tenant.objects.all()
    serializer_class = TenantSerializer
    permission_classes = [IsAuthenticated, IsPlatformStaff]


class PlanViewSet(AuditLoggedViewSetMixin, viewsets.ModelViewSet):
    """Lectura publica (sitio de marketing); escritura solo SUPER_ADMIN."""

    queryset = Plan.objects.all()
    serializer_class = PlanSerializer

    def get_permissions(self):
        if self.action in ("list", "retrieve"):
            return [AllowAny()]
        return [IsAuthenticated(), require_platform_role("SUPER_ADMIN")()]


class PlanFeatureViewSet(AuditLoggedViewSetMixin, viewsets.ModelViewSet):
    """Lectura: cualquier platform_staff (API Spec §2.5 solo restringe quien
    puede ESCRIBIR -antes este ViewSet exigia SUPER_ADMIN incluso para
    listar, lo que le impedia a SUPPORT/BILLING ver que trae cada plan)."""

    queryset = PlanFeature.objects.all()
    serializer_class = PlanFeatureSerializer

    def get_permissions(self):
        if self.action in ("list", "retrieve"):
            return [IsAuthenticated(), IsPlatformStaff()]
        return [IsAuthenticated(), require_platform_role("SUPER_ADMIN")()]


class SubscriptionViewSet(AuditLoggedViewSetMixin, viewsets.ModelViewSet):
    queryset = Subscription.objects.all()
    serializer_class = SubscriptionSerializer
    permission_classes = [IsAuthenticated, IsPlatformStaff]

    def get_queryset(self):
        queryset = super().get_queryset()
        tenant_id = self.request.query_params.get("tenant")
        if tenant_id:
            queryset = queryset.filter(tenant_id=tenant_id)
        status_param = self.request.query_params.get("status")
        if status_param:
            queryset = queryset.filter(status=status_param)
        return queryset


class SubscriptionPaymentViewSet(AuditLoggedViewSetMixin, viewsets.ModelViewSet):
    """Lectura: cualquier platform_staff. Escritura: solo BILLING."""

    queryset = SubscriptionPayment.objects.all()
    serializer_class = SubscriptionPaymentSerializer

    def get_permissions(self):
        if self.action in ("list", "retrieve"):
            return [IsAuthenticated(), IsPlatformStaff()]
        return [IsAuthenticated(), require_platform_role("BILLING")()]

    def get_queryset(self):
        queryset = super().get_queryset()
        subscription_id = self.request.query_params.get("subscription")
        if subscription_id:
            queryset = queryset.filter(subscription_id=subscription_id)
        status_param = self.request.query_params.get("status")
        if status_param:
            queryset = queryset.filter(status=status_param)
        return queryset


class SubscriptionPaymentConfirmView(APIView):
    """POST -> confirma manualmente un pago recibido por transferencia
    (Especificacion de API §4.10). Solo rol BILLING."""

    permission_classes = [IsAuthenticated, require_platform_role("BILLING")]

    def post(self, request, pk):
        payment = get_object_or_404(SubscriptionPayment, pk=pk)
        payment = SubscriptionPaymentService.confirm_payment(payment)
        PlatformAuditLogService.log_action(
            staff=request.user,
            action="PAYMENT_CONFIRMED",
            entity="SubscriptionPayment",
            entity_id=payment.id,
        )
        return Response(
            {"id": payment.id, "status": payment.status, "paid_at": payment.paid_at}
        )


class TenantSettingsViewSet(AuditLoggedViewSetMixin, viewsets.ModelViewSet):
    """Solo platform_staff por ahora. La Especificacion de API tambien permite
    'admin del propio tenant para toggles operativos', pero eso depende de
    PermissionService (usuarios), que llega recien en Sprint 2 -queda
    pendiente para entonces, no se improvisa aqui."""

    queryset = TenantSettings.objects.all()
    serializer_class = TenantSettingsSerializer
    permission_classes = [IsAuthenticated, IsPlatformStaff]

    def get_queryset(self):
        queryset = super().get_queryset()
        tenant_id = self.request.query_params.get("tenant")
        if tenant_id:
            queryset = queryset.filter(tenant_id=tenant_id)
        return queryset


class PlatformStaffViewSet(
    AuditLoggedViewSetMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    mixins.UpdateModelMixin,
    viewsets.GenericViewSet,
):
    """Gestion del equipo interno de Fivuza -restringido a SUPER_ADMIN tanto
    para lectura como escritura, dado que expone quien tiene cada rol
    interno (soporte/facturacion/administracion).

    Sin DestroyModelMixin (Sprint 9): platform_audit_logs.platform_staff es
    on_delete=PROTECT -un DELETE sobre un staff con historial de auditoria
    (el caso comun, ya que la bitacora registra cada accion) reventaria con
    un ProtectedError sin manejar. La API Spec pide "desactivacion en vez de
    borrado fisico"; se retira el borrado del CRUD en vez de agregarle un
    manejo de excepcion -is_active=False via PATCH ya cubre el caso de uso.
    """

    queryset = PlatformStaff.objects.all()
    serializer_class = PlatformStaffCRUDSerializer
    permission_classes = [IsAuthenticated, require_platform_role("SUPER_ADMIN")]


class PlatformAuditLogPagination(PageNumberPagination):
    page_size = 25
    page_size_query_param = "page_size"
    max_page_size = 100


class PlatformAuditLogViewSet(
    mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet
):
    """Solo lectura (Especificacion de API §4.14) -se escribe unicamente via
    PlatformAuditLogService.log_action(), nunca por POST/PUT del cliente.

    Filtros manuales por query params en vez de django-filter (Sprint 8): el
    set de filtros es chico y fijo (staff/entidad/rango de fechas), no
    amerita sumar una dependencia nueva al proyecto solo para esto."""

    queryset = PlatformAuditLog.objects.select_related("platform_staff").all()
    serializer_class = PlatformAuditLogSerializer
    permission_classes = [IsAuthenticated, IsPlatformStaff]
    pagination_class = PlatformAuditLogPagination
    filter_backends = [OrderingFilter]
    ordering_fields = ["created_at"]
    ordering = ["-created_at"]

    def get_queryset(self):
        queryset = super().get_queryset()
        params = self.request.query_params

        platform_staff = params.get("platform_staff")
        if platform_staff:
            queryset = queryset.filter(platform_staff_id=platform_staff)

        entity = params.get("entity")
        if entity:
            queryset = queryset.filter(entity=entity)

        entity_id = params.get("entity_id")
        if entity_id:
            queryset = queryset.filter(entity_id=entity_id)

        date_from = params.get("date_from")
        if date_from:
            queryset = queryset.filter(created_at__date__gte=date_from)

        date_to = params.get("date_to")
        if date_to:
            queryset = queryset.filter(created_at__date__lte=date_to)

        return queryset


class DashboardSummaryView(APIView):
    """GET -> resumen agregado del panel interno (Especificacion de API §4.13)."""

    permission_classes = [IsAuthenticated, IsPlatformStaff]

    def get(self, request):
        return Response(PlatformDashboardService.get_summary())


@api_view(["GET"])
@permission_classes([AllowAny])
def health_check(request):
    """
    Endpoint de monitoreo (Health Check).
    Verifica conexion real a PostgreSQL y Redis -no solo responde un valor fijo-
    para que un monitor externo (UptimeRobot/CloudWatch) detecte una caida real.
    """
    checks = {"database": _check_database(), "redis": _check_redis()}
    status_code = 200 if all(checks.values()) else 503
    return Response(
        {"status": "healthy" if status_code == 200 else "unhealthy", "checks": checks},
        status=status_code,
    )


def _check_database():
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        return True
    except OperationalError:
        return False


def _check_redis():
    try:
        client = redis.from_url(os.getenv("REDIS_URL", "redis://redis:6379/0"))
        return client.ping()
    except redis.RedisError:
        return False
