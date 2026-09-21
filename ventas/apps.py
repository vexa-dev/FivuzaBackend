from django.apps import AppConfig


class VentasConfig(AppConfig):
    name = "ventas"

    def ready(self):
        # Registra las señales del modulo (ver ventas/signals.py).
        from ventas import signals  # noqa: F401
