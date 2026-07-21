"""Señales de ventas.

post_save de Sale: push al dashboard en vivo vía Channels.
post_save de CashSession: dispara tasks.notify_cash_difference cuando
difference != 0 al cerrar la sesión.
"""
