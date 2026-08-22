from channels.generic.websocket import AsyncJsonWebsocketConsumer
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import UntypedToken


class DashboardConsumer(AsyncJsonWebsocketConsumer):
    """Empuja actualizaciones del dashboard en tiempo real (Sprint 24, TRD
    §2.5): una venta completada en el POS llega aquí vía
    DashboardBroadcastService sin que el cliente tenga que hacer polling.

    No pasa por TenantMainMiddleware (HTTP-only) ni por
    TenantValidatedJWTAuthentication (DRF-only) -el schema_name del tenant
    se resuelve leyendo el claim embebido en el propio JWT, recibido por
    query string (?token=...) porque el navegador no puede mandar un header
    Authorization en el handshake de WebSocket."""

    async def connect(self):
        token = self._get_token_from_query_string()
        access = await self._resolve_access(token)
        if access is None:
            await self.close(code=4001)
            return

        schema_name, is_admin, warehouse_ids = access
        self.group_names = (
            [f"dashboard_{schema_name}_all"]
            if is_admin
            else [
                f"dashboard_{schema_name}_warehouse_{warehouse_id}"
                for warehouse_id in warehouse_ids
            ]
        )
        for group_name in self.group_names:
            await self.channel_layer.group_add(group_name, self.channel_name)
        await self.accept()

    async def disconnect(self, close_code):
        for group_name in getattr(self, "group_names", []):
            await self.channel_layer.group_discard(group_name, self.channel_name)

    async def dashboard_event(self, event):
        # event["type"] == "dashboard.event" -> Channels llama a este metodo
        # (convierte el '.' en '_'). Se reenvia tal cual al cliente, sin el
        # campo "type" (detalle interno de enrutamiento de Channels).
        await self.send_json(
            {key: value for key, value in event.items() if key != "type"}
        )

    def _get_token_from_query_string(self) -> str | None:
        raw = self.scope.get("query_string", b"").decode()
        params = dict(pair.split("=", 1) for pair in raw.split("&") if "=" in pair)
        return params.get("token")

    @staticmethod
    async def _resolve_access(token: str | None):
        from asgiref.sync import sync_to_async
        from django_tenants.utils import get_tenant_model, schema_context

        if not token:
            return None
        try:
            validated = UntypedToken(token)
        except TokenError:
            return None

        schema_name = validated.get("schema_name")
        user_id = validated.get("user_id")
        if not schema_name or not user_id:
            return None

        tenant_model = get_tenant_model()
        exists = await sync_to_async(
            tenant_model.objects.filter(schema_name=schema_name).exists
        )()
        if not exists:
            return None

        def resolve_user_access():
            from usuarios.models import User
            from core.warehouse_access import WarehouseAccessService

            with schema_context(schema_name):
                try:
                    user = User.objects.select_related("role").get(
                        id=user_id, is_active=True
                    )
                except User.DoesNotExist:
                    return None
                return (
                    schema_name,
                    WarehouseAccessService.is_admin(user),
                    WarehouseAccessService.allowed_warehouse_ids(user),
                )

        return await sync_to_async(resolve_user_access, thread_sensitive=True)()
