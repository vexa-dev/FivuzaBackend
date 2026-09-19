from decimal import Decimal

from django.db.models import Q, Sum
from rest_framework.exceptions import APIException

from ventas.models import (
    CustomerBalanceLedger,
    CustomerDebtLedger,
    Sale,
)


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
    def register_payment(
        *, customer, amount: Decimal, description: str = ""
    ) -> CustomerDebtLedger:
        return CustomerDebtLedger.objects.create(
            customer=customer,
            type="CREDIT",
            amount=amount,
            description=description or "Abono de fiado",
        )
