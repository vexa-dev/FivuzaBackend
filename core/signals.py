from django.db.models.signals import post_save
from django.dispatch import receiver

from core.models import Tenant
from core.services import TenantProvisioningService


@receiver(post_save, sender=Tenant)
def provision_tenant(sender, instance, created, **kwargs):
    if created:
        TenantProvisioningService.provision(instance)
