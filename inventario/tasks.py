"""Tareas de Celery propias de inventario.

Incluye la creación mensual de la siguiente partición de inventory_movements,
y la notificación periódica de movimientos con oversell_flag=True pendientes
de reconciliación manual.
"""
