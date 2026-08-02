from django.db.models.signals import post_save
from django.dispatch import receiver
from django_tenants.signals import post_schema_sync

from core.models import Tenant
from core.services import TenantProvisioningService


@receiver(post_save, sender=Tenant)
def provision_tenant(sender, instance, created, **kwargs):
    if created:
        TenantProvisioningService.provision(instance)


@receiver(post_schema_sync)
def seed_default_roles_after_schema_sync(sender, tenant, **kwargs):
    """Se dispara recien cuando el esquema fisico del tenant ya existe y esta
    migrado -a diferencia de post_save de Tenant, que ocurre antes de que
    TenantMixin.save() cree el esquema (ver nota en
    TenantProvisioningService.provision()). Encola la siembra de roles en
    Celery (Sprint 8, Especificacion de API §4.9) en vez de ejecutarla aqui
    mismo, para no bloquear la respuesta HTTP del registro del tenant."""
    from core.tasks import provision_tenant_async

    provision_tenant_async.delay(tenant.id)
