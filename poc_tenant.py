import os
import django

# Configurar el entorno de Django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from core.models import Tenant, Domain

def create_tenants():
    print("Creando tenant public...")
    # Crear el tenant publico
    tenant = Tenant(schema_name='public',
                    company_name='Servicio Publico')
    tenant.save()

    # Add one or more domains for the tenant
    domain = Domain()
    domain.domain = 'public.localhost' # no incluyas el puerto ni www aqui
    domain.tenant = tenant
    domain.is_primary = True
    domain.save()
    print("Tenant public creado correctamente con dominio public.localhost")

    print("Creando tenant1 de prueba...")
    # Crear el primer tenant real
    tenant1 = Tenant(schema_name='tenant1',
                     company_name='Empresa de Prueba 1')
    tenant1.save() # migrate_schemas se llama automaticamente, el tenant estara listo

    # Add one or more domains for the tenant
    domain1 = Domain()
    domain1.domain = 'tenant1.localhost' # no incluyas el puerto ni www aqui
    domain1.tenant = tenant1
    domain1.is_primary = True
    domain1.save()
    print("Tenant tenant1 creado correctamente con dominio tenant1.localhost")

if __name__ == '__main__':
    try:
        create_tenants()
    except Exception as e:
        print(f"Error: {e}")
