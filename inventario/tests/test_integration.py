# Pruebas de flujo completo a través de la capa de servicios (ej. crear una venta
# de punta a punta), no solo de una unidad aislada.
from datetime import date
from decimal import Decimal

from django.utils import timezone
from django_tenants.test.cases import TenantTestCase

from core.models import TenantSettings
from dashboard.services import DashboardMetricsService
from inventario.models import (
    Category,
    PurchaseOrder,
    PurchaseOrderDetail,
    Stock,
    Supplier,
    Warehouse,
)
from inventario.services import ProductVariantService, PurchaseService, StockService
from usuarios.models import Role, User
from ventas.models import CashRegister, CashSession, Customer
from ventas.services import SaleService


class PurchaseThenSaleIntegrationTests(TenantTestCase):
    """Recibir una orden de compra (inventario) -> vender esa misma variante
    (ventas) -> el margen bruto del dashboard (dashboard) debe reflejar el
    costo promedio ponderado que dejo la compra, no el costo con el que la
    variante nacio. Cada pieza (PurchaseService.receive_order,
    SaleService.create_sale, DashboardMetricsService.gross_margin) ya tiene
    tests unitarios propios; esto prueba el cableado real entre las tres,
    que es justo lo que un test aislado no puede probar -si alguna cambia
    de forma incompatible con las otras, el margen calculado queda mal sin
    que ningun test unitario lo note."""

    @classmethod
    def get_test_schema_name(cls):
        return "test_inventario_purchase_then_sale"

    @classmethod
    def get_test_tenant_domain(cls):
        return "test-inventario-purchase-then-sale.test.com"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        role = Role.objects.get(name="admin")
        cls.user = User.objects.create(email="admin@negocio.com", role=role)
        cls.warehouse = Warehouse.objects.create(name="Principal")
        cls.supplier = Supplier.objects.create(
            ruc_or_dni="20123456789", company_name="Proveedor SAC"
        )
        cls.customer = Customer.objects.create(
            document_type="DNI", document_number="88888888", name="Cliente Integracion"
        )
        category = Category.objects.create(name="Ropa")
        # Costo inicial 0 a proposito: el unico costo que debe importar para
        # el margen es el que deja la compra, no el de creacion de la variante.
        product = ProductVariantService.create_product(
            product_data={
                "type": "PRODUCT",
                "name": "Camiseta",
                "category": category,
                "unit_of_measure": "UND",
            },
            variants_data=[
                {"sku": "SKU-INTEGRACION", "price": "50.00", "cost": "0.00"}
            ],
        )
        cls.variant = product.variants.first()

    @classmethod
    def tearDownClass(cls):
        TenantSettings.objects.filter(tenant=cls.tenant).delete()
        super().tearDownClass()

    def _receive_purchase(self, *, quantity, unit_cost):
        order = PurchaseOrder.objects.create(
            supplier=self.supplier,
            warehouse=self.warehouse,
            user=self.user,
            status="PENDING",
            total="0",
        )
        PurchaseOrderDetail.objects.create(
            purchase_order=order,
            variant_id=self.variant.id,
            quantity=quantity,
            unit_cost=unit_cost,
            subtotal=str(Decimal(quantity) * Decimal(unit_cost)),
        )
        return PurchaseService.receive_order(purchase_order=order, user=self.user)

    def _open_session(self):
        register = CashRegister.objects.create(warehouse=self.warehouse, name="Caja 1")
        return CashSession.objects.create(
            cash_register=register,
            user=self.user,
            opening_amount="0",
            opening_at=timezone.now(),
            status="OPEN",
        )

    def test_gross_margin_reflects_the_cost_left_by_the_purchase(self):
        self._receive_purchase(quantity="20", unit_cost="30.00")
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.cost, Decimal("30.0000"))

        session = self._open_session()
        SaleService.create_sale(
            customer=self.customer,
            cash_session=session,
            user=self.user,
            lines=[{"variant_id": self.variant.id, "quantity": "5"}],
            payments=[{"method": "CASH", "amount": Decimal("250.00")}],
        )

        stock = Stock.objects.get(variant=self.variant, warehouse=self.warehouse)
        self.assertEqual(stock.quantity, Decimal("15.000"))

        today = date.today()
        margin = DashboardMetricsService.gross_margin(date_from=today, date_to=today)
        # Ingreso: 5 x 50.00 = 250.00. Costo: 5 x 30.00 (el de la compra,
        # no el 0.00 con el que nacio la variante) = 150.00.
        self.assertEqual(margin["total_revenue"], "250.0000")
        self.assertEqual(margin["total_cost"], "150.0000")
        self.assertEqual(margin["gross_margin"], "100.0000")

    def test_a_second_purchase_reprices_future_sales_but_not_the_margin_of_past_ones(
        self,
    ):
        # Documenta la limitacion conocida de gross_margin (services.py):
        # calcula contra el costo ACTUAL de la variante, no el historico de
        # cuando se vendio -una compra posterior a la venta SI afecta el
        # margen ya reportado de esa venta si se vuelve a consultar.
        self._receive_purchase(quantity="10", unit_cost="10.00")
        session = self._open_session()
        SaleService.create_sale(
            customer=self.customer,
            cash_session=session,
            user=self.user,
            lines=[{"variant_id": self.variant.id, "quantity": "2"}],
            payments=[{"method": "CASH", "amount": Decimal("100.00")}],
        )

        today = date.today()
        margin_before = DashboardMetricsService.gross_margin(
            date_from=today, date_to=today
        )
        self.assertEqual(margin_before["total_cost"], "20.0000")

        # La venta ya bajo el stock a 8 antes de esta segunda compra -el
        # promedio ponderado pesa contra ese stock restante, no contra las
        # 10 unidades originales: (10.00*8 + 50.00*10) / 18 = 32.2222.
        self._receive_purchase(quantity="10", unit_cost="50.00")
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.cost, Decimal("32.2222"))

        margin_after = DashboardMetricsService.gross_margin(
            date_from=today, date_to=today
        )
        self.assertEqual(margin_after["total_cost"], "64.4444")

    def test_transfer_stock_between_warehouses_does_not_change_variant_cost(self):
        self._receive_purchase(quantity="10", unit_cost="15.00")
        self.variant.refresh_from_db()
        cost_before = self.variant.cost

        secondary_warehouse = Warehouse.objects.create(name="Sucursal")
        StockService.transfer_stock(
            variant=self.variant,
            from_warehouse=self.warehouse,
            to_warehouse=secondary_warehouse,
            quantity=Decimal("4"),
            user=self.user,
        )

        self.variant.refresh_from_db()
        self.assertEqual(self.variant.cost, cost_before)
        self.assertEqual(
            Stock.objects.get(variant=self.variant, warehouse=self.warehouse).quantity,
            Decimal("6.000"),
        )
        self.assertEqual(
            Stock.objects.get(
                variant=self.variant, warehouse=secondary_warehouse
            ).quantity,
            Decimal("4.000"),
        )
