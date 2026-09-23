from datetime import timedelta

from django.db import IntegrityError
from django.utils import timezone
from rest_framework.exceptions import APIException

from core.warehouse_access import WarehouseAccessService
from ventas.models import (
    CashSession,
    Customer,
    Sale,
)
from ventas.services.sales import SaleService


class SyncReferenceNotFoundError(APIException):
    """Una venta del lote offline referencia un cliente o una caja que ya no
    existe. Solo falla esa venta (status FAILED en la respuesta)."""

    status_code = 404
    default_code = "SYNC_REFERENCE_NOT_FOUND"

    def __init__(self, code: str, message: str):
        super().__init__(
            detail={"error": {"code": code, "message": message, "details": {}}},
            code=code,
        )


class SaleSyncService:
    """POST /ventas/sales/sync/ (Sprint 20, API Spec §4.2): procesa el lote
    de ventas que el POS acumulo sin conexion. Idempotente por
    client_side_uuid -reenviar el mismo lote completo (ej. la app reintenta
    porque el request anterior se corto a mitad de la respuesta) nunca
    duplica una venta ni descuenta stock una segunda vez. Cada venta del
    lote es su propia unidad: create_sale() ya es @transaction.atomic por
    venta, asi que una falla en una no revierte ni bloquea las demas."""

    # Tolerancia al reloj del dispositivo: una hora "futura" de pocos minutos
    # es desfase normal; mas alla, se usa la hora del servidor.
    _MAX_CLOCK_SKEW = timedelta(minutes=5)

    @staticmethod
    def _occurred_at(value):
        now = timezone.now()
        if value is None or value > now + SaleSyncService._MAX_CLOCK_SKEW:
            return now
        return min(value, now)

    @staticmethod
    def _resolve_references(sale_data: dict, user):
        """Resuelve cliente y caja de UNA venta del lote. Cada error es un
        APIException con codigo propio, asi sync_batch() marca solo esta
        venta como FAILED y sigue con las demas."""
        customer = Customer.objects.filter(id=sale_data["customer_id"]).first()
        if customer is None:
            raise SyncReferenceNotFoundError(
                "CUSTOMER_NOT_FOUND", "El cliente de esta venta ya no existe."
            )
        cash_session = (
            CashSession.objects.select_related("cash_register")
            .filter(id=sale_data["cash_session_id"])
            .first()
        )
        if cash_session is None:
            raise SyncReferenceNotFoundError(
                "CASH_SESSION_NOT_FOUND", "La caja de esta venta ya no existe."
            )
        WarehouseAccessService.require_warehouse(
            user, cash_session.cash_register.warehouse_id
        )
        return customer, cash_session

    @staticmethod
    def sync_batch(*, sales: list[dict], user) -> dict:
        synced = []
        conflicts = []

        for sale_data in sales:
            client_side_uuid = sale_data["client_side_uuid"]
            existing = Sale.objects.filter(client_side_uuid=client_side_uuid).first()
            if existing is not None:
                synced.append(
                    {
                        "client_side_uuid": client_side_uuid,
                        "status": "DUPLICATE_IGNORED",
                        "sale_id": existing.id,
                    }
                )
                continue

            try:
                customer, cash_session = SaleSyncService._resolve_references(
                    sale_data, user
                )
                sale = SaleService.create_sale(
                    customer=customer,
                    cash_session=cash_session,
                    user=user,
                    lines=sale_data["lines"],
                    payments=sale_data["payments"],
                    client_side_uuid=client_side_uuid,
                    allow_oversell=True,
                    at=SaleSyncService._occurred_at(sale_data.get("occurred_at")),
                    # Bloque C.2: sin conexion no hay a quien pedir
                    # autorizacion; el POS ya impide pasar el tope offline,
                    # asi que lo que llegue por encima se registra y se
                    # marca en la bitacora en vez de perder la venta.
                    discount_limit="flag",
                )
            except IntegrityError:
                # Dos sincronizaciones casi simultaneas del mismo
                # client_side_uuid (ej. reintento de red del mismo device) -
                # el chequeo de arriba no alcanzo a verla, pero el
                # constraint unico de la tabla si.
                existing = Sale.objects.get(client_side_uuid=client_side_uuid)
                synced.append(
                    {
                        "client_side_uuid": client_side_uuid,
                        "status": "DUPLICATE_IGNORED",
                        "sale_id": existing.id,
                    }
                )
                continue
            except APIException as exc:
                synced.append(
                    {
                        "client_side_uuid": client_side_uuid,
                        "status": "FAILED",
                        "error": exc.detail,
                    }
                )
                continue

            synced.append(
                {
                    "client_side_uuid": client_side_uuid,
                    "status": "CREATED",
                    "sale_id": sale.id,
                }
            )
            for variant_id in sale.oversold_variant_ids:
                conflicts.append(
                    {
                        "client_side_uuid": client_side_uuid,
                        "variant_id": variant_id,
                        "oversell_flag": True,
                    }
                )

        return {"synced": synced, "conflicts": conflicts}
