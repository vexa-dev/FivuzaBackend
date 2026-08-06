# Pruebas de concurrencia sobre SaleService.create_sale() (Sprint 15, TRD
# §7.2): dos ventas simultaneas sobre la misma variante nunca deben dejar el
# stock en negativo, ni perder el select_for_update() que StockService ya
# probo en inventario/tests/test_concurrency.py -aqui se verifica que
# SaleService hereda esa misma proteccion al reusar StockService.adjust_stock
# dentro de su propia transaccion atomica.
import threading
from decimal import Decimal

from django.db import close_old_connections, connection
from django.test import TransactionTestCase
from django_tenants.utils import schema_context

from core.models import Tenant, TenantSettings
from inventario.models import Category, Stock, Warehouse
from inventario.services import ProductVariantService, StockService
from usuarios.models import Role, User
from ventas.models import CashRegister, CashSession, Customer
from ventas.services import InsufficientStockError, SaleService


class SaleServiceConcurrencyTests(TransactionTestCase):
    reset_sequences = True

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tenant = Tenant.objects.create(
            schema_name="test_sale_concurrency",
            company_name="Negocio Concurrencia Venta",
        )
        with schema_context(cls.tenant.schema_name):
            role = Role.objects.create(name="admin", is_system_default=True)
            cls.user = User.objects.create(email="admin@negocio.com", role=role)
            cls.warehouse = Warehouse.objects.create(name="Principal")
            category = Category.objects.create(name="Ropa")
            product = ProductVariantService.create_product(
                product_data={
                    "type": "PRODUCT",
                    "name": "Camiseta",
                    "category": category,
                    "unit_of_measure": "UND",
                },
                variants_data=[{"sku": "SALE-CONCURRENCY-SKU", "price": "10.00"}],
            )
            cls.variant = product.variants.first()
            StockService.adjust_stock(
                variant=cls.variant,
                warehouse=cls.warehouse,
                counted_quantity=5,
                concept="ADJUSTMENT",
                user=cls.user,
            )
            cls.customer = Customer.objects.create(
                document_type="DNI",
                document_number="99999999",
                name="Cliente Concurrencia",
            )
            cls.register = CashRegister.objects.create(
                warehouse=cls.warehouse, name="Caja Concurrencia"
            )

    @classmethod
    def tearDownClass(cls):
        with schema_context(cls.tenant.schema_name):
            TenantSettings.objects.filter(tenant=cls.tenant).delete()
        cls.tenant.delete(force_drop=True)
        super().tearDownClass()

    def test_two_concurrent_sales_never_oversell(self):
        """Stock inicial = 5. Dos hilos intentan vender 3 unidades cada uno
        (6 en total, mas de lo disponible) sobre la MISMA variante -sin el
        lock heredado de StockService.adjust_stock, ambos leerian stock=5 y
        los dos pasarian la validacion, dejando el stock en -1. Con el lock,
        el segundo hilo espera al primero y ve el stock ya descontado, asi
        que falla con InsufficientStockError en vez de vender de mas."""
        results: dict[str, str] = {}
        barrier = threading.Barrier(2)

        def sell(thread_name: str):
            close_old_connections()
            with schema_context(self.tenant.schema_name):
                session = CashSession.objects.create(
                    cash_register=self.register,
                    user=self.user,
                    opening_amount="0",
                    opening_at="2026-01-01T00:00:00Z",
                    status="OPEN",
                )
                barrier.wait(timeout=5)
                try:
                    SaleService.create_sale(
                        customer=self.customer,
                        cash_session=session,
                        user=self.user,
                        lines=[{"variant_id": self.variant.id, "quantity": "3"}],
                        payments=[{"method": "CASH", "amount": Decimal("30.00")}],
                    )
                    results[thread_name] = "sold"
                except InsufficientStockError:
                    results[thread_name] = "rejected"
                finally:
                    connection.close()

        t1 = threading.Thread(target=sell, args=("t1",))
        t2 = threading.Thread(target=sell, args=("t2",))
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        with schema_context(self.tenant.schema_name):
            final_stock = Stock.objects.get(
                variant=self.variant, warehouse=self.warehouse
            )

        self.assertEqual(len(results), 2)
        self.assertEqual(list(results.values()).count("sold"), 1)
        self.assertEqual(list(results.values()).count("rejected"), 1)
        self.assertEqual(final_stock.quantity, Decimal("2.000"))
