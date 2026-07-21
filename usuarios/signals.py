"""Señales de usuarios.

pre_delete de User: verifica que no sea el último admin activo del tenant
antes de permitir is_active=False (siempre debe quedar al menos un admin).
"""
