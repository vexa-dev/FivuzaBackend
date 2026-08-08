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
        schema_name = await self._resolve_schema(token)
        if schema_name is None:
            await self.close(code=4001)
            return

        self.group_name = f"dashboard_{schema_name}"
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()

    async def disconnect(self, close_code):
        if hasattr(self, "group_name"):
            await self.channel_layer.group_discard(self.group_name, self.channel_name)

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
    async def _resolve_schema(token: str | None) -> str | None:
        from asgiref.sync import sync_to_async
        from django_tenants.utils import get_tenant_model

        if not token:
            return None
        try:
            validated = UntypedToken(token)
        except TokenError:
            return None

        schema_name = validated.get("schema_name")
        if not schema_name:
            return None

        tenant_model = get_tenant_model()
        exists = await sync_to_async(
            tenant_model.objects.filter(schema_name=schema_name).exists
        )()
        return schema_name if exists else None
