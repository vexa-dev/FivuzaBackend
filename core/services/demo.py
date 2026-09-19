import random
import uuid
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone
from rest_framework.exceptions import APIException

from core.models import (
    Tenant,
)


class TenantNotDemoError(APIException):
    status_code = 400
    default_code = "TENANT_NOT_DEMO"
    default_detail = {
        "error": {
            "code": "TENANT_NOT_DEMO",
            "message": "Esta operacion solo puede ejecutarse sobre un tenant de demostracion (is_demo=True).",
        }
    }


class DemoTenantService:
    """Ambiente de demostracion comercial (Sprint 32): seed_demo_tenant()
    puebla un tenant con datos ficticios realistas para que una reunion
    comercial no arranque con un catalogo vacio; reset_demo_tenant() lo
    devuelve a un estado limpio y conocido antes de la siguiente reunion.

    Ambos metodos se niegan a correr si tenant.is_demo no es True -es la
    unica salvaguarda entre un error de configuracion (ej. pasar el id de
    tenant equivocado) y borrar o resiembrar los datos de un negocio real.

    [DECISION] Las ventas historicas se insertan directamente via el ORM
    (bulk_create + bulk_update para retro-fechar created_at, que ignora el
    valor pasado en auto_now_add), no a traves de SaleService: simular
    cientos de transacciones reales una por una es lento e innecesario
    para datos que solo necesitan verse realistas en pantalla, no
    reconstruir el historial exacto de movimientos de stock."""

    _CATEGORY_NAMES = [
        "Ropa",
        "Calzado",
        "Accesorios",
        "Electronica",
        "Hogar",
        "Deportes",
    ]
    _PRODUCT_BASE_NAMES = [
        "Camiseta",
        "Polo",
        "Pantalon Jean",
        "Pantalon Jogger",
        "Casaca",
        "Zapatillas",
        "Sandalias",
        "Gorra",
        "Mochila",
        "Billetera",
        "Reloj",
        "Audifonos",
        "Cargador",
        "Termo",
        "Lampara",
        "Silla",
        "Mesa",
        "Balon",
        "Mancuerna",
        "Bicicleta",
    ]
    _EMPLOYEE_POSITIONS = ["Cajero", "Vendedor", "Almacenero", "Supervisor"]

    @staticmethod
    def _require_demo(tenant: Tenant) -> None:
        if not tenant.is_demo:
            raise TenantNotDemoError()

    @staticmethod
    def seed_demo_tenant(
        tenant: Tenant,
        *,
        product_count: int = 200,
        customer_count: int = 30,
        employee_count: int = 8,
        sale_count: int = 300,
        months_of_history: int = 6,
    ) -> dict:
        DemoTenantService._require_demo(tenant)

        from django_tenants.utils import schema_context

        with schema_context(tenant.schema_name):
            return DemoTenantService._seed(
                product_count=product_count,
                customer_count=customer_count,
                employee_count=employee_count,
                sale_count=sale_count,
                months_of_history=months_of_history,
            )

    @staticmethod
    def reset_demo_tenant(tenant: Tenant, **seed_kwargs) -> dict:
        DemoTenantService._require_demo(tenant)

        from django_tenants.utils import schema_context

        with schema_context(tenant.schema_name):
            DemoTenantService._wipe()
            return DemoTenantService._seed(**seed_kwargs)

    @staticmethod
    def _wipe() -> None:
        # Orden de borrado que respeta on_delete=PROTECT: hijos antes que
        # padres (Sale antes que Customer, Stock antes que ProductVariant,
        # ProductVariant antes que Product).
        from inventario.models import Product, ProductVariant, Stock
        from usuarios.models import Employee
        from ventas.models import CashSession, Customer, Sale

        Sale.objects.all().delete()
        CashSession.objects.all().delete()
        Stock.objects.all().delete()
        ProductVariant.all_objects.all().delete()
        Product.all_objects.all().delete()
        Customer.objects.all().delete()
        Employee.all_objects.all().delete()

    @staticmethod
    def _seed(
        *,
        product_count: int = 200,
        customer_count: int = 30,
        employee_count: int = 8,
        sale_count: int = 300,
        months_of_history: int = 6,
    ) -> dict:
        from inventario.models import (
            Category,
            Product,
            ProductVariant,
            Stock,
            Warehouse,
        )
        from usuarios.models import Employee, User
        from ventas.models import (
            CashRegister,
            CashSession,
            Customer,
            Sale,
            SaleDetail,
            SalePayment,
        )

        warehouse, _ = Warehouse.objects.get_or_create(
            name="Principal", defaults={"is_active": True}
        )
        cash_register, _ = CashRegister.objects.get_or_create(
            warehouse=warehouse, name="Caja Principal"
        )
        admin_user = (
            User.objects.filter(role__name="admin", is_active=True).first()
            or User.objects.filter(is_active=True).first()
        )

        categories = [
            Category.objects.get_or_create(name=name)[0]
            for name in DemoTenantService._CATEGORY_NAMES
        ]

        products = Product.objects.bulk_create(
            [
                Product(
                    type="PRODUCT",
                    name=f"{DemoTenantService._PRODUCT_BASE_NAMES[i % len(DemoTenantService._PRODUCT_BASE_NAMES)]} {i // len(DemoTenantService._PRODUCT_BASE_NAMES) + 1}",
                    category=random.choice(categories),
                    unit_of_measure="UND",
                )
                for i in range(product_count)
            ]
        )

        new_variants = []
        for i, product in enumerate(products):
            cost = round(random.uniform(5, 150), 2)
            new_variants.append(
                ProductVariant(
                    product=product,
                    sku=f"DEMO-{i:05d}",
                    cost=cost,
                    price=round(cost * random.uniform(1.3, 2.0), 2),
                )
            )
        variants = ProductVariant.objects.bulk_create(new_variants)

        Stock.objects.bulk_create(
            [
                Stock(
                    variant=variant,
                    warehouse=warehouse,
                    quantity=random.randint(5, 100),
                )
                for variant in variants
            ]
        )

        customers = Customer.objects.bulk_create(
            [
                Customer(
                    document_type="DNI",
                    document_number=f"9{i:07d}",
                    name=f"Cliente Demo {i + 1}",
                )
                for i in range(customer_count)
            ]
        )

        Employee.objects.bulk_create(
            [
                Employee(
                    full_name=f"Empleado Demo {i + 1}",
                    document_number=f"8{i:07d}",
                    position=DemoTenantService._EMPLOYEE_POSITIONS[
                        i % len(DemoTenantService._EMPLOYEE_POSITIONS)
                    ],
                    warehouse=warehouse,
                    salary_type="MONTHLY",
                    salary_amount=round(random.uniform(1200, 3500), 2),
                    hire_date=timezone.localdate()
                    - timedelta(days=random.randint(30, 900)),
                )
                for i in range(employee_count)
            ]
        )

        # Sesiones de caja cerradas: una por mes de historial, con montos
        # ficticios pero razonables (no ligadas a ventas individuales -no
        # hace falta reconciliar centavo a centavo para que se vea real).
        if admin_user is not None:
            cash_sessions = []
            for month_offset in range(months_of_history):
                opening_at = timezone.now() - timedelta(days=30 * (month_offset + 1))
                opening_amount = Decimal("200.00")
                counted = opening_amount + Decimal(
                    str(round(random.uniform(300, 1500), 2))
                )
                cash_sessions.append(
                    CashSession(
                        cash_register=cash_register,
                        user=admin_user,
                        opening_amount=opening_amount,
                        opening_at=opening_at,
                        expected_closing_amount=counted,
                        counted_closing_amount=counted,
                        difference=Decimal("0"),
                        status="CLOSED",
                        closing_at=opening_at + timedelta(hours=9),
                    )
                )
            CashSession.objects.bulk_create(cash_sessions)

        # Ventas historicas repartidas en los ultimos `months_of_history`
        # meses -bulk_create + bulk_update porque auto_now_add ignora
        # cualquier valor de created_at pasado directamente al crear.
        # bulk_create() sobre Postgres devuelve las instancias con pk ya
        # asignado, asi que no hace falta volver a consultarlas -evita que
        # el detalle de la venta termine referenciando una variante distinta
        # a la que se uso para calcular su total.
        sales = []
        sale_specs = []
        if admin_user is not None and customers and variants:
            for i in range(sale_count):
                variant = random.choice(variants)
                quantity = random.randint(1, 3)
                subtotal = variant.price * quantity
                sales.append(
                    Sale(
                        invoice_number=f"DEMO-{i:06d}",
                        customer=random.choice(customers),
                        user=admin_user,
                        warehouse=warehouse,
                        subtotal=subtotal,
                        total=subtotal,
                        payment_status="PAID",
                        status="COMPLETED",
                        client_side_uuid=str(uuid.uuid4()),
                        sync_status="SYNCED",
                    )
                )
                sale_specs.append((variant, quantity, subtotal))
            sales = Sale.objects.bulk_create(sales)

            for sale in sales:
                sale.created_at = timezone.now() - timedelta(
                    days=random.randint(0, 30 * months_of_history)
                )
            Sale.objects.bulk_update(sales, ["created_at"])

            details = []
            payments = []
            for sale, (variant, quantity, subtotal) in zip(sales, sale_specs):
                details.append(
                    SaleDetail(
                        sale=sale,
                        variant_id=variant.id,
                        product_name_snapshot=variant.product.name,
                        sku_snapshot=variant.sku,
                        quantity=quantity,
                        unit_price=variant.price,
                        subtotal=subtotal,
                    )
                )
                payments.append(
                    SalePayment(sale=sale, method="CASH", amount=sale.total)
                )
            SaleDetail.objects.bulk_create(details)
            SalePayment.objects.bulk_create(payments)

        return {
            "products": len(products),
            "variants": len(variants),
            "customers": len(customers),
            "employees": employee_count,
            "sales": len(sales),
        }
