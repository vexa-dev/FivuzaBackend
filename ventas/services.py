from django.db.models import Sum
from django.utils import timezone
from rest_framework.exceptions import APIException

from ventas.models import CashMovement, CashRegister, CashSession


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


class CashSessionService:
    """Apertura/cierre de caja con arqueo (Especificacion de API §4.4;
    Esquema Backend §7.2). Una caja fisica (CashRegister) no puede tener dos
    sesiones abiertas a la vez -es la regla que hace que "que caja esta
    usando cada cajero ahora mismo" sea una pregunta con una sola respuesta."""

    @staticmethod
    def open_session(
        *, cash_register: CashRegister, user, opening_amount
    ) -> CashSession:
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
        *, session: CashSession, counted_closing_amount, user, notes: str | None = None
    ) -> CashSession:
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

        return session

    @staticmethod
    def _calculate_expected_closing_amount(session: CashSession):
        # Ventas al contado (SalePayment.method=CASH) todavia no se pueden
        # crear -SaleService llega en un sprint posterior- pero la relacion
        # Sale.cash_session ya existe en el modelo (BDD v5), asi que se
        # incluye desde ya: el dia que el POS exista, el arqueo ya calcula
        # bien sin tocar este metodo.
        from ventas.models import SalePayment

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
    def add_movement(
        *, session: CashSession, type: str, concept: str, amount, user
    ) -> CashMovement:
        if session.status != "OPEN":
            raise CashSessionNotOpenError()

        return CashMovement.objects.create(
            cash_session=session, type=type, concept=concept, amount=amount, user=user
        )
