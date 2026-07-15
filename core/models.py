from django.db import models
from django_tenants.models import TenantMixin, DomainMixin


class Tenant(TenantMixin):
    company_name = models.CharField(max_length=100)
    created_on = models.DateField(auto_now_add=True)

    # Por defecto en True: el esquema se creará y sincronizará automáticamente al guardar
    auto_create_schema = True

    def __str__(self):
        return self.company_name


class Domain(DomainMixin):
    pass
