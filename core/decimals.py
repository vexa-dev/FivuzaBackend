"""Regla unica de decimales del sistema: todo numero con decimales (montos,
precios, costos, cantidades, peso y porcentajes) se guarda y se calcula con
2 decimales, redondeando medio hacia arriba (1.575 -> 1.58, 12.957 ->
12.96), que es el redondeo habitual de una boleta.

Todo calculo que pueda dejar mas de 2 decimales (un producto, un porcentaje,
una division) pasa por round2() ANTES de guardarse o compararse: la base
redondearia sola al guardar, pero el valor en memoria (con el que se compara
el pago contra el total, PAYMENT_MISMATCH) seguiria con fracciones. El
frontend aplica la misma regla (src/shared/utils/decimals.ts).
"""

from decimal import ROUND_HALF_UP, Decimal

DECIMAL_PLACES = 2
CENT = Decimal("0.01")


def round2(value: Decimal | int | str) -> Decimal:
    """Redondea a 2 decimales, medio hacia arriba (lejos del cero en los
    empates, tambien para negativos: -1.575 -> -1.58)."""
    return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)
