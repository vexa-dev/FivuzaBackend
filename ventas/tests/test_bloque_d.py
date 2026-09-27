# Bloque D (Plan de Mejoras Operativas): cliente de paso (D.1), pago
# enriquecido (D.2), estado de pago real (D.4), devoluciones segun el medio
# de cobro (D.5) y conciliacion (D.6).
from decimal import Decimal

from django.utils import timezone
from django_tenants.test.cases import TenantTestCase
from rest_framework.exceptions import ValidationError

from core.models import TenantSettings
from inventario.models import Category
from inventario.services import ProductVariantService, StockService
from usuarios.models import Role, User
from ventas.models import Customer, CashRegister, CashSession, SalePayment
from ventas.services import (
    CreditLedgerService,
    ReturnService,
    SaleService,
    SettlementImportService,
    WalkInCustomerNotAllowedError,
)


class BloqueDTests(TenantTestCase):
    @classmethod
    def get_test_schema_name(cls):
        return "test_ventas_bloque_d"

    @classmethod
    def get_test_tenant_domain(cls):
        return "test-ventas-bloque-d.test.com"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        role = Role.objects.get(name="admin")
        cls.user = User.objects.create(email="admin@negocio.com", role=role)
        from inventario.models import Warehouse

        cls.warehouse = Warehouse.objects.create(name="Principal")
        cls.category = Category.objects.create(name="Ropa")
        cls.customer = Customer.objects.create(
            document_type="DNI", document_number="33333333", name="Cliente Dos"
        )
        # Sembrado por la migracion de datos 0014_seed_walkin_customer.
        cls.walk_in = Customer.objects.get(is_walk_in=True)

    @classmethod
    def tearDownClass(cls):
        TenantSettings.objects.filter(tenant=cls.tenant).delete()
        super().tearDownClass()

    _sku_counter = 0

    def _create_variant(self, *, price="20.00", stock_quantity="10"):
        BloqueDTests._sku_counter += 1
        product = ProductVariantService.create_product(
            product_data={
                "type": "PRODUCT",
                "name": "Camiseta",
                "category": self.category,
                "unit_of_measure": "UND",
            },
            variants_data=[
                {"sku": f"SKU-BLOQUE-D-{BloqueDTests._sku_counter}", "price": price}
            ],
        )
        variant = product.variants.first()
        StockService.adjust_stock(
            variant=variant,
            warehouse=self.warehouse,
            counted_quantity=Decimal(stock_quantity),
            concept="ADJUSTMENT",
            user=self.user,
        )
        return variant

    def _open_session(self):
        BloqueDTests._sku_counter += 1
        register = CashRegister.objects.create(
            warehouse=self.warehouse, name=f"Caja {BloqueDTests._sku_counter}"
        )
        return CashSession.objects.create(
            cash_register=register,
            user=self.user,
            opening_amount="0",
            opening_at=timezone.now(),
            status="OPEN",
        )

    # --- D.1: cliente de paso ---------------------------------------------

    def test_walk_in_customer_can_pay_cash(self):
        variant = self._create_variant()
        session = self._open_session()

        sale = SaleService.create_sale(
            customer=self.walk_in,
            cash_session=session,
            user=self.user,
            lines=[{"variant_id": variant.id, "quantity": "1"}],
            payments=[{"method": "CASH", "amount": Decimal("20.00")}],
        )

        self.assertEqual(sale.payment_status, "PAID")

    def test_walk_in_customer_cannot_buy_on_credit(self):
        variant = self._create_variant()
        session = self._open_session()

        with self.assertRaises(WalkInCustomerNotAllowedError):
            SaleService.create_sale(
                customer=self.walk_in,
                cash_session=session,
                user=self.user,
                lines=[{"variant_id": variant.id, "quantity": "1"}],
                payments=[{"method": "CREDIT_LEDGER", "amount": Decimal("20.00")}],
            )

    def test_walk_in_customer_cannot_pay_with_balance(self):
        variant = self._create_variant()
        session = self._open_session()

        with self.assertRaises(WalkInCustomerNotAllowedError):
            SaleService.create_sale(
                customer=self.walk_in,
                cash_session=session,
                user=self.user,
                lines=[{"variant_id": variant.id, "quantity": "1"}],
                payments=[{"method": "BALANCE", "amount": Decimal("20.00")}],
            )

    # --- D.2: pago enriquecido ---------------------------------------------

    def test_card_payment_requires_operation_number(self):
        variant = self._create_variant()
        session = self._open_session()

        with self.assertRaises(ValidationError):
            SaleService.create_sale(
                customer=self.customer,
                cash_session=session,
                user=self.user,
                lines=[{"variant_id": variant.id, "quantity": "1"}],
                payments=[{"method": "CARD", "amount": Decimal("20.00")}],
            )

    def test_duplicate_operation_number_warns_but_does_not_block(self):
        variant = self._create_variant(stock_quantity="20")
        session = self._open_session()
        payment = {
            "method": "CARD",
            "amount": Decimal("20.00"),
            "provider": "visanet",
            "operation_number": "OP-100",
        }

        first = SaleService.create_sale(
            customer=self.customer,
            cash_session=session,
            user=self.user,
            lines=[{"variant_id": variant.id, "quantity": "1"}],
            payments=[payment],
        )
        self.assertEqual(first.payment_warnings, [])

        second = SaleService.create_sale(
            customer=self.customer,
            cash_session=session,
            user=self.user,
            lines=[{"variant_id": variant.id, "quantity": "1"}],
            payments=[payment],
        )
        self.assertEqual(len(second.payment_warnings), 1)

    def test_invalid_card_last4_is_rejected(self):
        variant = self._create_variant()
        session = self._open_session()

        with self.assertRaises(ValidationError):
            SaleService.create_sale(
                customer=self.customer,
                cash_session=session,
                user=self.user,
                lines=[{"variant_id": variant.id, "quantity": "1"}],
                payments=[
                    {
                        "method": "CARD",
                        "amount": Decimal("20.00"),
                        "operation_number": "OP-1",
                        "card_last4": "12345",
                    }
                ],
            )

    # --- D.4: estado de pago real -------------------------------------------

    def test_partial_credit_sale_settles_to_paid_on_full_payment(self):
        variant = self._create_variant(stock_quantity="20")
        session = self._open_session()

        sale = SaleService.create_sale(
            customer=self.customer,
            cash_session=session,
            user=self.user,
            lines=[{"variant_id": variant.id, "quantity": "1"}],
            payments=[
                {"method": "CASH", "amount": Decimal("5.00")},
                {"method": "CREDIT_LEDGER", "amount": Decimal("15.00")},
            ],
        )
        self.assertEqual(sale.payment_status, "PARTIAL")
        self.assertEqual(sale.credit_amount, Decimal("15.00"))

        CreditLedgerService.register_payment(customer=self.customer, amount=Decimal("15.00"))
        sale.refresh_from_db()
        self.assertEqual(sale.payment_status, "PAID")

    def test_credit_payment_allocates_fifo_across_oldest_sales_first(self):
        variant = self._create_variant(stock_quantity="20")
        session = self._open_session()

        older = SaleService.create_sale(
            customer=self.customer,
            cash_session=session,
            user=self.user,
            lines=[{"variant_id": variant.id, "quantity": "1"}],
            payments=[{"method": "CREDIT_LEDGER", "amount": Decimal("20.00")}],
        )
        newer = SaleService.create_sale(
            customer=self.customer,
            cash_session=session,
            user=self.user,
            lines=[{"variant_id": variant.id, "quantity": "1"}],
            payments=[{"method": "CREDIT_LEDGER", "amount": Decimal("20.00")}],
        )
        self.assertEqual(older.payment_status, "UNPAID")
        self.assertEqual(newer.payment_status, "UNPAID")

        # Solo alcanza para saldar la mas antigua.
        CreditLedgerService.register_payment(customer=self.customer, amount=Decimal("20.00"))
        older.refresh_from_db()
        newer.refresh_from_db()
        self.assertEqual(older.payment_status, "PAID")
        self.assertEqual(newer.payment_status, "UNPAID")

    # --- D.5: devoluciones segun el medio de cobro --------------------------

    def test_return_defaults_to_balance_when_sale_was_paid_electronically(self):
        variant = self._create_variant()
        session = self._open_session()
        sale = SaleService.create_sale(
            customer=self.customer,
            cash_session=session,
            user=self.user,
            lines=[{"variant_id": variant.id, "quantity": "1"}],
            payments=[
                {"method": "CARD", "amount": Decimal("20.00"), "operation_number": "OP-9"}
            ],
        )
        detail = sale.details.first()

        sale_return = ReturnService.create_return(
            sale=sale,
            items=[{"sale_detail_id": detail.id, "quantity_returned": "1"}],
            reason="No le gustó",
            refund_type=None,
            user=self.user,
        )
        self.assertEqual(sale_return.refund_type, "BALANCE")

    def test_return_defaults_to_cash_when_sale_was_paid_in_cash(self):
        variant = self._create_variant()
        session = self._open_session()
        sale = SaleService.create_sale(
            customer=self.customer,
            cash_session=session,
            user=self.user,
            lines=[{"variant_id": variant.id, "quantity": "1"}],
            payments=[{"method": "CASH", "amount": Decimal("20.00")}],
        )
        detail = sale.details.first()

        sale_return = ReturnService.create_return(
            sale=sale,
            items=[{"sale_detail_id": detail.id, "quantity_returned": "1"}],
            reason="No le gustó",
            refund_type=None,
            user=self.user,
            cash_session=session,
        )
        self.assertEqual(sale_return.refund_type, "CASH")

    # --- D.6: conciliacion ---------------------------------------------------

    def test_settlement_import_matches_existing_payment_and_flags_unmatched(self):
        variant = self._create_variant()
        session = self._open_session()
        SaleService.create_sale(
            customer=self.customer,
            cash_session=session,
            user=self.user,
            lines=[{"variant_id": variant.id, "quantity": "1"}],
            payments=[
                {
                    "method": "CARD",
                    "amount": Decimal("20.00"),
                    "provider": "visanet",
                    "operation_number": "OP-SETTLE-1",
                }
            ],
        )

        csv_content = (
            "numero_operacion,monto,comision\n"
            "OP-SETTLE-1,20.00,0.60\n"
            "OP-NO-EXISTE,15.00,0.45\n"
        )
        report = SettlementImportService.import_csv(
            file_content=csv_content,
            provider="visanet",
            period_start="2026-09-01",
            period_end="2026-09-30",
            total_deposited=Decimal("35.00"),
            total_fee=Decimal("1.05"),
            user=self.user,
        )

        self.assertEqual(report["matched"], 1)
        self.assertEqual(report["unmatched"], 1)
        self.assertEqual(report["errors"], 0)

        matched_payment = SalePayment.objects.get(operation_number="OP-SETTLE-1")
        self.assertIsNotNone(matched_payment.settled_at)
        self.assertEqual(matched_payment.fee_amount, Decimal("0.60"))
