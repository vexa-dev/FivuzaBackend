from django_tenants.test.cases import TenantTestCase
from rest_framework.exceptions import ValidationError

from core.models import TenantSettings
from inventario.models import Warehouse
from usuarios.models import Role, User, UserWarehouse
from usuarios.warehouse_access import WarehouseAccessDenied, WarehouseAccessService


class WarehouseAccessServiceTests(TenantTestCase):
    @classmethod
    def get_test_schema_name(cls):
        return "test_warehouse_access"

    @classmethod
    def get_test_tenant_domain(cls):
        return "test-warehouse-access.test.com"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.primary = Warehouse.objects.get(name="Principal")
        cls.secondary = Warehouse.objects.create(name="Secundario")
        cls.admin = User.objects.create(
            email="admin-access@example.com", role=Role.objects.get(name="admin")
        )
        manager_role = Role.objects.get(name="manager")
        cls.restricted = User.objects.create(
            email="restricted@example.com", role=manager_role
        )
        cls.unassigned = User.objects.create(
            email="unassigned@example.com", role=manager_role
        )
        UserWarehouse.objects.create(user=cls.restricted, warehouse=cls.primary)

    @classmethod
    def tearDownClass(cls):
        TenantSettings.objects.filter(tenant=cls.tenant).delete()
        super().tearDownClass()

    def test_system_admin_has_access_to_every_warehouse_without_assignments(self):
        self.assertTrue(WarehouseAccessService.is_admin(self.admin))
        self.assertCountEqual(
            WarehouseAccessService.allowed_warehouse_ids(self.admin),
            [self.primary.id, self.secondary.id],
        )

    def test_restricted_user_only_receives_assigned_warehouses(self):
        self.assertEqual(
            WarehouseAccessService.allowed_warehouse_ids(self.restricted),
            (self.primary.id,),
        )
        scoped = WarehouseAccessService.scope_queryset(
            Warehouse.objects.all(), self.restricted, lookup="id"
        )
        self.assertEqual(list(scoped.values_list("id", flat=True)), [self.primary.id])

    def test_unassigned_non_admin_has_no_operational_access(self):
        self.assertEqual(
            WarehouseAccessService.allowed_warehouse_ids(self.unassigned), ()
        )
        self.assertFalse(
            WarehouseAccessService.scope_queryset(
                Warehouse.objects.all(), self.unassigned, lookup="id"
            ).exists()
        )

    def test_explicit_unauthorized_warehouse_raises_stable_error(self):
        with self.assertRaises(WarehouseAccessDenied) as context:
            WarehouseAccessService.require_warehouse(self.restricted, self.secondary.id)
        self.assertEqual(context.exception.default_code, "WAREHOUSE_ACCESS_DENIED")

    def test_non_numeric_warehouse_id_raises_validation_error_not_admin(self):
        with self.assertRaises(ValidationError):
            WarehouseAccessService.require_warehouse(self.admin, "no-es-numero")

    def test_non_numeric_warehouse_id_raises_validation_error_scoped_user(self):
        with self.assertRaises(ValidationError):
            WarehouseAccessService.require_warehouse(self.restricted, "no-es-numero")
