import uuid
from decimal import Decimal

from django.db.models import Sum
from django.utils import timezone
from rest_framework.exceptions import APIException

from core import storage
from ventas.models import (
    CashMovement,
    CashRegister,
    CashSession,
    SalePayment,
)


# Comprobantes de movimientos de caja: mismo patron de URL prefirmada de S3
# que inventario.services.MediaService, pero self-contenido aqui -un
# CashMovement no existe todavia cuando se pide la URL (a diferencia de una
# ProductVariant, que ya tiene id antes de subir su imagen), asi que la key
# se genera con un uuid propio en vez de depender de un pk existente.
_PRESIGNED_URL_TTL_SECONDS = 300


_ALLOWED_RECEIPT_CONTENT_TYPES = {
    "image/jpeg",
    "image/png",
    "image/webp",
    "application/pdf",
}


class CashSessionAlreadyOpenError(APIException):
    status_code = 409
    default_code = "CASH_SESSION_ALREADY_OPEN"
    default_detail = {
        "error": {
            "code": "CASH_SESSION_ALREADY_OPEN",
            "message": "Esta caja ya tiene una sesion abierta.",
        }
    }


class CashSessionNotOpenError(APIException):
    status_code = 409
    default_code = "CASH_SESSION_NOT_OPEN"
    default_detail = {
        "error": {
            "code": "CASH_SESSION_NOT_OPEN",
            "message": "Esta sesion de caja ya esta cerrada.",
        }
    }


class CashSessionNotOwnedError(APIException):
    status_code = 403
    default_code = "CASH_SESSION_NOT_OWNED"
    default_detail = {
        "error": {
            "code": "CASH_SESSION_NOT_OWNED",
            "message": "Esta caja no es tuya. Pide a quien la abrio, o a un supervisor, que la opere.",
        }
    }


class CashSessionService:
    """Apertura/cierre de caja con arqueo (Especificacion de API §4.4;
    Esquema Backend §7.2). Una caja fisica (CashRegister) no puede tener dos
    sesiones abiertas a la vez -es la regla que hace que "que caja esta
    usando cada cajero ahora mismo" sea una pregunta con una sola respuesta.

    Bloque A.2 agrega la otra mitad de esa pregunta: de quien es la caja.
    - Si CashRegister.assigned_user esta puesto, la caja es de esa persona:
      solo ella (o quien tenga CASH_CLOSE) abre, vende y cierra en ella.
    - Si no lo esta, quien abre el turno es el dueño de esa sesion: solo esa
      persona vende y cierra.
    - Quien tenga CASH_CLOSE siempre puede cerrar, para el caso real de "el
      cajero se fue sin cerrar".
    """

    @staticmethod
    def _has_cash_close(user) -> bool:
        from usuarios.services import PermissionService

        return PermissionService.check_effective_permission(user, "CASH_CLOSE")

    @staticmethod
    def assert_can_open(*, cash_register: CashRegister, user) -> None:
        assigned_id = cash_register.assigned_user_id
        if assigned_id is None or assigned_id == user.id:
            return
        if CashSessionService._has_cash_close(user):
            return
        raise CashSessionNotOwnedError()

    @staticmethod
    def assert_can_sell(*, session: CashSession, user) -> None:
        """La venta es el caso estricto: ni siquiera un supervisor vende en
        la caja de otro sin que la caja este asignada a el -meter ventas
        ajenas en un turno es exactamente lo que descuadra el arqueo."""
        assigned_id = session.cash_register.assigned_user_id
        if assigned_id is not None:
            if assigned_id == user.id or CashSessionService._has_cash_close(user):
                return
            raise CashSessionNotOwnedError()
        if session.user_id != user.id:
            raise CashSessionNotOwnedError()

    @staticmethod
    def assert_can_close(*, session: CashSession, user) -> None:
        assigned_id = session.cash_register.assigned_user_id
        owner_id = assigned_id if assigned_id is not None else session.user_id
        if owner_id == user.id:
            return
        if CashSessionService._has_cash_close(user):
            return
        raise CashSessionNotOwnedError()

    @staticmethod
    def open_session(
        *, cash_register: CashRegister, user, opening_amount
    ) -> CashSession:
        CashSessionService.assert_can_open(cash_register=cash_register, user=user)
        if CashSession.objects.filter(
            cash_register=cash_register, status="OPEN"
        ).exists():
            raise CashSessionAlreadyOpenError()

        return CashSession.objects.create(
            cash_register=cash_register,
            user=user,
            opening_amount=opening_amount,
            opening_at=timezone.now(),
            status="OPEN",
        )

    @staticmethod
    def close_session(
        *,
        session: CashSession,
        counted_closing_amount,
        user,
        tenant=None,
        notes: str | None = None,
    ) -> CashSession:
        CashSessionService.assert_can_close(session=session, user=user)
        if session.status != "OPEN":
            raise CashSessionNotOpenError()

        expected = CashSessionService._calculate_expected_closing_amount(session)
        session.expected_closing_amount = expected
        session.counted_closing_amount = counted_closing_amount
        session.difference = counted_closing_amount - expected
        session.status = "CLOSED"
        session.closing_at = timezone.now()
        if notes:
            session.notes = notes
        session.save(
            update_fields=[
                "expected_closing_amount",
                "counted_closing_amount",
                "difference",
                "status",
                "closing_at",
                "notes",
            ]
        )

        from usuarios.services import AuditLogService

        AuditLogService.log_action(
            user=user,
            action="CASH_SESSION_CLOSED",
            entity="CashSession",
            entity_id=session.id,
            details={
                "expected_closing_amount": str(expected),
                "counted_closing_amount": str(counted_closing_amount),
                "difference": str(session.difference),
            },
        )

        if tenant is not None:
            CashSessionService._maybe_alert_on_difference(
                session=session, tenant=tenant
            )

        return session

    @staticmethod
    def _maybe_alert_on_difference(*, session: CashSession, tenant) -> None:
        from core.models import TenantSettings

        threshold = TenantSettings.objects.get(
            tenant=tenant
        ).cash_difference_alert_threshold
        if abs(session.difference) <= threshold:
            return

        from ventas.tasks import send_cash_difference_alert

        send_cash_difference_alert.delay(tenant.schema_name, session.id)

    @staticmethod
    def _calculate_expected_closing_amount(session: CashSession):
        # Ventas en efectivo de la sesion. Una venta anulada no se resta
        # aqui: void_sale() registra su propio egreso de caja.

        cash_sales = (
            SalePayment.objects.filter(
                method="CASH", sale__cash_session=session
            ).aggregate(total=Sum("amount"))["total"]
            or 0
        )
        movements_in = (
            session.movements.filter(type="IN").aggregate(total=Sum("amount"))["total"]
            or 0
        )
        movements_out = (
            session.movements.filter(type="OUT").aggregate(total=Sum("amount"))["total"]
            or 0
        )
        return session.opening_amount + cash_sales + movements_in - movements_out

    @staticmethod
    def payment_totals_by_method(session: CashSession) -> dict[str, str]:
        """Bloque A.4: cuanto entro por cada medio de pago en el turno.

        El esperado del arqueo solo cuenta efectivo (es lo unico que hay en
        el cajon), pero el cierre necesita mostrar tambien lo cobrado por
        tarjeta, Yape, fiado y saldo -el pendiente "desglose por metodo de
        pago" que el modulo de Caja arrastra desde el Sprint 12. Las ventas
        anuladas no se excluyen aqui: el efectivo devuelto ya sale como
        movimiento OUT de DEVOLUCION, y para el resto de medios el negocio
        quiere ver lo que efectivamente paso por el turno.
        """
        totals = {
            method: Decimal("0")
            for method, _ in SalePayment._meta.get_field("method").choices
        }
        rows = (
            SalePayment.objects.filter(sale__cash_session=session)
            .values("method")
            .annotate(total=Sum("amount"))
        )
        for row in rows:
            totals[row["method"]] = row["total"] or Decimal("0")
        return {method: str(total) for method, total in totals.items()}

    @staticmethod
    def add_movement(
        *,
        session: CashSession,
        type: str,
        concept: str,
        amount,
        user,
        reason: str = "",
        receipt_url: str | None = None,
    ) -> CashMovement:
        if session.status != "OPEN":
            raise CashSessionNotOpenError()

        return CashMovement.objects.create(
            cash_session=session,
            type=type,
            concept=concept,
            amount=amount,
            user=user,
            reason=reason,
            receipt_url=receipt_url,
        )


class CashMovementReceiptService:
    """URLs prefirmadas de S3 para el comprobante de un movimiento de caja
    (Convenciones §5.1) -mismo patron que inventario.services.MediaService."""

    @staticmethod
    def build_receipt_upload_url(content_type: str) -> dict:
        if content_type not in _ALLOWED_RECEIPT_CONTENT_TYPES:
            raise ValueError(f"Tipo de archivo no permitido: {content_type}")

        extension = content_type.split("/")[-1]
        key = f"cash-movement-receipts/{uuid.uuid4()}.{extension}"

        upload_url = storage.presigned_upload_url(
            key, content_type, _PRESIGNED_URL_TTL_SECONDS
        )
        return {
            "upload_url": upload_url,
            "receipt_url": storage.public_object_url(key),
        }
