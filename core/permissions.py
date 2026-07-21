"""Permisos DRF compartidos entre las 4 apps de negocio.

Incluye el permission_classes que verifica tenants.status antes de procesar
cualquier request de negocio -si está suspended, responde 402 sin tocar datos.
"""
