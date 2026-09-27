from decimal import Decimal

from django.db.models import F, Q, Sum
from rest_framework.exceptions import APIException

from ventas.models import (
    CustomerBalanceLedger,
    CustomerDebtLedger,
    Sale,
)


class WalkInCustomerNotAllowedError(APIException):
    """Bloque D.1: el cliente de paso no puede fiar ni tener saldo a favor
    -no se le puede cobrar ni reclamar despues, y no aparece en Cobranzas."""

    status_code = 409

    def __init__(self, *, default_code: str, message: str):
        self.default_code = default_code
        super().__init__({"error": {"code": default_code, "message": message}})


class CreditLimitExceededError(APIException):
    status_code = 409
    default_code = "CREDIT_LIMIT_EXCEEDED"

    def __init__(
        self, *, current_debt: Decimal, credit_limit: Decimal, requested: Decimal
    ):
        super().__init__(
            {
                "error": {
                    "code": "CREDIT_LIMIT_EXCEEDED",
                    "message": (
                        f"El cliente supera su limite de credito: deuda actual "
                        f"{current_debt}, limite {credit_limit}, solicitado {requested}."
                    ),
                }
            }
        )


class InsufficientBalanceError(APIException):
    status_code = 409
    default_code = "INSUFFICIENT_BALANCE"

    def __init__(self, *, available: Decimal, requested: Decimal):
        super().__init__(
            {
                "error": {
                    "code": "INSUFFICIENT_BALANCE",
                    "message": (
                        f"El cliente no tiene suficiente saldo a favor: "
                        f"disponible {available}, solicitado {requested}."
                    ),
                }
            }
        )


class CreditLedgerService:
    """Unico punto de entrada a CustomerDebtLedger/CustomerBalanceLedger
    (Sprint 19, Plan de Implementacion): el saldo nunca se calcula ni se
    escribe fuera de aca -ni SaleService.create_sale() ni SaleService.
    void_sale() tocan esos modelos directamente, todos pasan por estos
    metodos. Es lo que faltaba desde el Sprint 18 (ver docstring de
    void_sale): ahora que este servicio existe, la anulacion de una venta a
    credito o pagada con saldo a favor si revierte esos libros."""

    @staticmethod
    def get_debt(customer) -> Decimal:
        totals = CustomerDebtLedger.objects.filter(customer=customer).aggregate(
            debit=Sum("amount", filter=Q(type="DEBIT")),
            credit=Sum("amount", filter=Q(type="CREDIT")),
        )
        return (totals["debit"] or Decimal("0")) - (totals["credit"] or Decimal("0"))

    @staticmethod
    def get_balance(customer) -> Decimal:
        totals = CustomerBalanceLedger.objects.filter(customer=customer).aggregate(
            credit=Sum("amount", filter=Q(type="CREDIT")),
            debit=Sum("amount", filter=Q(type="DEBIT")),
        )
        return (totals["credit"] or Decimal("0")) - (totals["debit"] or Decimal("0"))

    @staticmethod
    def register_credit_sale(
        *, customer, sale: Sale, amount: Decimal
    ) -> CustomerDebtLedger:
        if customer.is_walk_in:
            raise WalkInCustomerNotAllowedError(
                default_code="WALK_IN_CREDIT_NOT_ALLOWED",
                message="El cliente de paso no puede fiar: registra un cliente real.",
            )
        current_debt = CreditLedgerService.get_debt(customer)
        if (
            customer.credit_limit is not None
            and current_debt + amount > customer.credit_limit
        ):
            raise CreditLimitExceededError(
                current_debt=current_debt,
                credit_limit=customer.credit_limit,
                requested=amount,
            )
        return CustomerDebtLedger.objects.create(
            customer=customer,
            sale=sale,
            type="DEBIT",
            amount=amount,
            description=f"Venta a credito {sale.invoice_number}",
        )

    @staticmethod
    def register_balance_use(
        *, customer, sale: Sale, amount: Decimal
    ) -> CustomerBalanceLedger:
        if customer.is_walk_in:
            raise WalkInCustomerNotAllowedError(
                default_code="WALK_IN_BALANCE_NOT_ALLOWED",
                message=(
                    "El cliente de paso no tiene saldo a favor: registra un "
                    "cliente real."
                ),
            )
        current_balance = CreditLedgerService.get_balance(customer)
        if amount > current_balance:
            raise InsufficientBalanceError(available=current_balance, requested=amount)
        return CustomerBalanceLedger.objects.create(
            customer=customer,
            sale=sale,
            type="DEBIT",
            amount=amount,
            description=f"Pago con saldo a favor, venta {sale.invoice_number}",
        )

    @staticmethod
    def reverse_credit_sale(
        *, customer, sale: Sale, amount: Decimal
    ) -> CustomerDebtLedger:
        """Anulacion de una venta a credito (SaleService.void_sale): el saldo
        adeudado por esa venta se perdona, nunca se borra el DEBIT original
        -mismo principio de "nunca editar/borrar" que el resto del proyecto."""
        return CustomerDebtLedger.objects.create(
            customer=customer,
            sale=sale,
            type="CREDIT",
            amount=amount,
            description=f"Anulacion de venta {sale.invoice_number}",
        )

    @staticmethod
    def reverse_balance_use(
        *, customer, sale: Sale, amount: Decimal
    ) -> CustomerBalanceLedger:
        return CustomerBalanceLedger.objects.create(
            customer=customer,
            sale=sale,
            type="CREDIT",
            amount=amount,
            description=f"Anulacion de venta {sale.invoice_number}",
        )

    @staticmethod
    def register_balance_credit(
        *, customer, sale_return, amount: Decimal, description: str = ""
    ) -> CustomerBalanceLedger:
        """Alta de saldo a favor por una devolucion (Bloque D.5): reemplaza
        el alta directa que ReturnService hacia antes, para que este servicio
        siga siendo el unico punto de entrada al libro de saldo."""
        return CustomerBalanceLedger.objects.create(
            customer=customer,
            sale_return=sale_return,
            sale=sale_return.sale,
            type="CREDIT",
            amount=amount,
            description=description or f"Devolución {sale_return.sale.invoice_number}",
        )

    @staticmethod
    def register_payment(
        *, customer, amount: Decimal, description: str = ""
    ) -> CustomerDebtLedger:
        """Abono de fiado. Bloque D.4: ademas del asiento contable, asigna el
        abono FIFO contra las ventas mas antiguas del cliente con parte
        fiada pendiente (Sale.credit_amount > credit_settled_amount), para
        que payment_status refleje que ya se pago -sin esto una venta a
        credito nunca deja de figurar como pendiente aunque el cliente ya
        abono todo."""
        entry = CustomerDebtLedger.objects.create(
            customer=customer,
            type="CREDIT",
            amount=amount,
            description=description or "Abono de fiado",
        )
        remaining = amount
        pending_sales = Sale.objects.filter(
            customer=customer, credit_amount__gt=F("credit_settled_amount")
        ).order_by("occurred_at")
        for sale in pending_sales:
            if remaining <= 0:
                break
            outstanding = sale.credit_amount - sale.credit_settled_amount
            applied = min(remaining, outstanding)
            sale.credit_settled_amount += applied
            sale.payment_status = (
                "PAID" if sale.credit_settled_amount >= sale.credit_amount else "PARTIAL"
            )
            sale.save(update_fields=["credit_settled_amount", "payment_status"])
            remaining -= applied
        return entry
