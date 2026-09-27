"""Conciliacion de cobros electronicos (Bloque D.6): el negocio carga el
archivo de liquidacion del operador (tarjeta/Yape) con el periodo, el total
depositado y la comision, y el sistema cruza cada linea contra los
SalePayment ya cobrados por numero de operacion y monto. Mismo patron que
inventario.services.CatalogImportService (Sprint 6): valida y confirma fila
por fila, cada una en su propio savepoint, y deja un solo registro de
auditoria por import con el resumen total."""

import csv
import io
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from ventas.models import PaymentSettlement, PaymentSettlementLine, SalePayment

_SETTLEMENT_IMPORT_HEADERS = ["numero_operacion", "monto", "comision"]
# Tolerancia de redondeo al cruzar monto del archivo vs. SalePayment.amount:
# el operador a veces reporta con un centavo de diferencia por su propio
# redondeo interno, no porque el cobro este mal.
_AMOUNT_TOLERANCE = Decimal("0.01")

# Cuantos dias sin conciliar antes de que un cobro aparezca como "sin
# deposito" en vez de simplemente "todavia no llego la liquidacion".
UNRECONCILED_AFTER_DAYS = 15


class SettlementImportService:
    @staticmethod
    def build_template_csv() -> str:
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(_SETTLEMENT_IMPORT_HEADERS)
        writer.writerow(["OP-000123", "150.00", "4.50"])
        return buffer.getvalue()

    @staticmethod
    def import_csv(
        *,
        file_content: str,
        provider: str,
        period_start,
        period_end,
        total_deposited: Decimal,
        total_fee: Decimal,
        user,
    ) -> dict:
        reader = csv.DictReader(io.StringIO(file_content))
        missing = set(_SETTLEMENT_IMPORT_HEADERS) - set(reader.fieldnames or [])
        if missing:
            raise ValidationError(
                f"Faltan columnas en el archivo: {', '.join(sorted(missing))}"
            )

        rows = list(reader)
        settlement = PaymentSettlement.objects.create(
            provider=provider,
            period_start=period_start,
            period_end=period_end,
            total_deposited=total_deposited,
            total_fee=total_fee,
            uploaded_by=user,
        )

        results = []
        for index, row in enumerate(rows, start=2):  # la fila 1 es el encabezado
            operation_number = (row.get("numero_operacion") or "").strip()
            error = SettlementImportService._validate_row(row, operation_number)
            if error:
                results.append(
                    {
                        "row": index,
                        "operation_number": operation_number,
                        "status": "error",
                        "error": error,
                    }
                )
                continue

            try:
                with transaction.atomic():
                    line_status = SettlementImportService._create_row(
                        settlement=settlement, row=row, operation_number=operation_number
                    )
            except Exception as exc:  # noqa: BLE001 -- una fila mala no debe frenar el resto del archivo
                results.append(
                    {
                        "row": index,
                        "operation_number": operation_number,
                        "status": "error",
                        "error": str(exc),
                    }
                )
                continue

            results.append(
                {
                    "row": index,
                    "operation_number": operation_number,
                    "status": line_status,
                }
            )

        matched = sum(1 for r in results if r["status"] == "MATCHED")
        unmatched = sum(1 for r in results if r["status"] == "UNMATCHED_DEPOSIT")
        errors = sum(1 for r in results if r["status"] == "error")

        # Un registro por import, no uno por linea (mismo criterio que
        # CATALOG_IMPORTED, Bloque B.4).
        from usuarios.services import AuditLogService

        AuditLogService.log_action(
            user=user,
            action="SETTLEMENT_IMPORTED",
            entity="PaymentSettlement",
            entity_id=settlement.id,
            details={
                "provider": provider,
                "total": len(rows),
                "matched": matched,
                "unmatched": unmatched,
                "errors": errors,
            },
        )

        return {
            "settlement_id": settlement.id,
            "total": len(rows),
            "matched": matched,
            "unmatched": unmatched,
            "errors": errors,
            "rows": results,
        }

    @staticmethod
    def _validate_row(row: dict, operation_number: str) -> str | None:
        if not operation_number:
            return "numero_operacion es requerido"
        try:
            Decimal(row.get("monto") or "0")
        except InvalidOperation:
            return "monto invalido"
        comision = row.get("comision")
        if comision:
            try:
                Decimal(comision)
            except InvalidOperation:
                return "comision invalida"
        return None

    @staticmethod
    def _create_row(*, settlement: PaymentSettlement, row: dict, operation_number: str) -> str:
        amount = Decimal(row["monto"])
        fee_amount = Decimal(row.get("comision") or "0")

        match = (
            SalePayment.objects.select_for_update()
            .filter(
                method__in=["CARD", "YAPE"],
                provider=settlement.provider,
                operation_number=operation_number,
                amount__gte=amount - _AMOUNT_TOLERANCE,
                amount__lte=amount + _AMOUNT_TOLERANCE,
            )
            .exclude(status="VOIDED")
            .first()
        )
        status_value = "MATCHED" if match else "UNMATCHED_DEPOSIT"
        PaymentSettlementLine.objects.create(
            settlement=settlement,
            operation_number=operation_number,
            amount=amount,
            fee_amount=fee_amount,
            matched_sale_payment=match,
            status=status_value,
        )
        if match:
            match.settled_at = timezone.now()
            match.fee_amount = fee_amount
            match.save(update_fields=["settled_at", "fee_amount"])
        return status_value

    @staticmethod
    def reconciliation_report(*, provider: str | None = None) -> dict:
        """Las tres listas que pide D.6: conciliados, cobros sin deposito
        (SalePayment de tarjeta/Yape sin settled_at, pasado el umbral) y
        depositos sin cobro (lineas de liquidacion que no cruzaron)."""
        cutoff = timezone.now() - timezone.timedelta(days=UNRECONCILED_AFTER_DAYS)

        matched_lines = PaymentSettlementLine.objects.filter(status="MATCHED")
        unmatched_lines = PaymentSettlementLine.objects.filter(
            status="UNMATCHED_DEPOSIT"
        )
        payments_without_deposit = SalePayment.objects.filter(
            method__in=["CARD", "YAPE"],
            settled_at__isnull=True,
            created_at__lt=cutoff,
        ).exclude(status="VOIDED")

        if provider:
            matched_lines = matched_lines.filter(settlement__provider=provider)
            unmatched_lines = unmatched_lines.filter(settlement__provider=provider)
            payments_without_deposit = payments_without_deposit.filter(
                provider=provider
            )

        return {
            "reconciled": list(matched_lines.values(
                "id", "operation_number", "amount", "fee_amount", "matched_sale_payment"
            )),
            "deposits_without_payment": list(unmatched_lines.values(
                "id", "operation_number", "amount", "fee_amount", "settlement"
            )),
            "payments_without_deposit": list(payments_without_deposit.values(
                "id", "sale_id", "provider", "operation_number", "amount", "created_at"
            )),
        }
