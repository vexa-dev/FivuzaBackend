from django.utils import timezone


class SoftDeleteDestroyMixin:
    """DELETE como baja logica: marca deleted_at/deleted_by (y apaga
    is_active si el modelo lo tiene) en vez de borrar la fila.

    Antes cada ViewSet repetia estas cuatro lineas en su propio
    perform_destroy; como mixin, queda debajo de usuarios.audit.
    TenantAuditMixin en el MRO y la baja sale en la bitacora sin que cada
    vista tenga que acordarse."""

    def perform_destroy(self, instance):
        instance.deleted_at = timezone.now()
        instance.deleted_by = self.request.user
        update_fields = ["deleted_at", "deleted_by"]
        if any(field.name == "is_active" for field in instance._meta.concrete_fields):
            instance.is_active = False
            update_fields.append("is_active")
        instance.save(update_fields=update_fields)
