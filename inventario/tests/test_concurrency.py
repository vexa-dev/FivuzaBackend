# Pruebas de concurrencia sobre StockService (Sprint 4, TRD §7.2): dos
# ajustes simultaneos sobre la misma variante+almacen no pueden dejar el
# stock inconsistente. TenantTestCase envuelve cada test en una transaccion
# atomica que nunca se comitea -eso bloquearia para siempre a un segundo
# hilo esperando el select_for_update() del primero, porque ese hilo jamas
# veria los datos de setUp. Por eso esta prueba usa TransactionTestCase
# (comitea de verdad) y arma su propio tenant a mano, en vez de heredar de
# TenantTestCase.
import threading

from django.db import close_old_connections, connection
from django.test import TransactionTestCase
from django_tenants.utils import schema_context

from core.models import Tenant, TenantSettings
from inventario.models import Category, Stock, Warehouse
from inventario.services import ProductVariantService, StockService
from usuarios.models import Role, User


class StockServiceConcurrencyTests(TransactionTestCase):
    reset_sequences = True

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tenant = Tenant.objects.create(
            schema_name="test_stock_concurrency", company_name="Negocio Concurrencia"
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
                variants_data=[{"sku": "CONCURRENCY-SKU"}],
            )
            cls.variant = product.variants.first()
            StockService.adjust_stock(
                variant=cls.variant,
                warehouse=cls.warehouse,
                counted_quantity=100,
                concept="ADJUSTMENT",
                user=cls.user,
            )

    @classmethod
    def tearDownClass(cls):
        with schema_context(cls.tenant.schema_name):
            TenantSettings.objects.filter(tenant=cls.tenant).delete()
        cls.tenant.delete(force_drop=True)
        super().tearDownClass()

    def test_two_concurrent_adjustments_never_lose_an_update(self):
        """Dos hilos ajustan la MISMA variante+almacen al mismo tiempo, a
        valores distintos. select_for_update() debe serializarlos -sin el
        lock, ambos leerian stock=100 y uno de los dos ajustes se perderia
        (clasico lost update). Con el lock, el segundo hilo espera a que el
        primero termine y ve su resultado como punto de partida."""
        results = {}
        barrier = threading.Barrier(2)

        def adjust(thread_name: str, counted_quantity: int):
            close_old_connections()
            with schema_context(self.tenant.schema_name):
                barrier.wait(timeout=5)
                try:
                    movement = StockService.adjust_stock(
                        variant=self.variant,
                        warehouse=self.warehouse,
                        counted_quantity=counted_quantity,
                        concept="ADJUSTMENT",
                        user=self.user,
                    )
                    results[thread_name] = movement.resulting_balance
                finally:
                    connection.close()

        t1 = threading.Thread(target=adjust, args=("t1", 80))
        t2 = threading.Thread(target=adjust, args=("t2", 60))
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        with schema_context(self.tenant.schema_name):
            final_stock = Stock.objects.get(
                variant=self.variant, warehouse=self.warehouse
            )

        # El valor final es el del hilo que escribio ultimo -lo importante
        # no es CUAL gana, sino que el resultado sea consistente con uno de
        # los dos ajustes (nunca un valor intermedio corrupto ni un delta
        # calculado sobre datos obsoletos).
        self.assertIn(final_stock.quantity, (80, 60))
        self.assertEqual(len(results), 2)
