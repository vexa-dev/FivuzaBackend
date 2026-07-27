# Pruebas de modelos: validaciones de campo, constraints, métodos del modelo.
from django_tenants.test.cases import TenantTestCase

from core.models import TenantSettings
from usuarios.models import (
    AuditLog,
    Permission,
    Role,
    RolePermission,
    User,
    UserPermission,
)
from usuarios.services import AuditLogService, PermissionService, RoleService


class PermissionServiceTests(TenantTestCase):
    @classmethod
    def get_test_schema_name(cls):
        return "test_permission_service"

    @classmethod
    def get_test_tenant_domain(cls):
        return "test-permission-service.test.com"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.role = Role.objects.create(name="seller")
        cls.perm_sell = Permission.objects.create(code="SALES_CREATE", module="SALES")
        cls.perm_view = Permission.objects.create(code="SALES_VIEW", module="SALES")
        RolePermission.objects.create(role=cls.role, permission=cls.perm_sell)
        cls.user = User.objects.create(email="vendedor@negocio.com", role=cls.role)

    @classmethod
    def tearDownClass(cls):
        TenantSettings.objects.filter(tenant=cls.tenant).delete()
        super().tearDownClass()

    def setUp(self):
        # La cache de permisos vive en Redis, fuera de la transaccion de
        # base de datos que Django revierte al terminar cada test -sin este
        # limpiado, un valor cacheado por un test anterior (calculado sobre
        # un estado de BD que luego se revirtio) contaminaria este test.
        from django.core.cache import cache

        cache.clear()

    def test_user_has_permission_inherited_from_role(self):
        self.assertTrue(PermissionService.check_permission(self.user, "SALES_CREATE"))
        self.assertFalse(PermissionService.check_permission(self.user, "SALES_VIEW"))

    def test_individual_override_grants_extra_permission(self):
        UserPermission.objects.create(
            user=self.user, permission=self.perm_view, is_granted=True
        )
        PermissionService.invalidate_user_cache(self.user.id)
        self.assertTrue(PermissionService.check_permission(self.user, "SALES_VIEW"))

    def test_individual_override_revokes_role_permission(self):
        UserPermission.objects.create(
            user=self.user, permission=self.perm_sell, is_granted=False
        )
        PermissionService.invalidate_user_cache(self.user.id)
        self.assertFalse(PermissionService.check_permission(self.user, "SALES_CREATE"))

    def test_result_is_cached_until_invalidated(self):
        self.assertTrue(PermissionService.check_permission(self.user, "SALES_CREATE"))

        # Revocar el permiso del rol directamente (sin pasar por RoleService)
        # no deberia reflejarse hasta invalidar la cache manualmente -es
        # exactamente el comportamiento de cache que se esta probando.
        RolePermission.objects.filter(
            role=self.role, permission=self.perm_sell
        ).delete()
        self.assertTrue(PermissionService.check_permission(self.user, "SALES_CREATE"))

        PermissionService.invalidate_user_cache(self.user.id)
        self.assertFalse(PermissionService.check_permission(self.user, "SALES_CREATE"))

    def test_invalidate_role_cache_affects_all_users_of_that_role(self):
        other_user = User.objects.create(email="otro@negocio.com", role=self.role)
        self.assertTrue(PermissionService.check_permission(other_user, "SALES_CREATE"))

        RolePermission.objects.filter(
            role=self.role, permission=self.perm_sell
        ).delete()
        PermissionService.invalidate_role_cache(self.role.id)

        self.assertFalse(PermissionService.check_permission(self.user, "SALES_CREATE"))
        self.assertFalse(PermissionService.check_permission(other_user, "SALES_CREATE"))


class RoleServiceTests(TenantTestCase):
    @classmethod
    def get_test_schema_name(cls):
        return "test_role_service"

    @classmethod
    def get_test_tenant_domain(cls):
        return "test-role-service.test.com"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Rol y permiso propios del test, con un code que no colisiona con
        # el catalogo base que TenantProvisioningService.seed_default_roles()
        # ya sembro al crear el tenant (post_schema_sync).
        cls.role = Role.objects.create(name="rol_de_prueba")
        cls.permission = Permission.objects.create(
            code="TEST_ROLE_SERVICE_PERM", module="HR"
        )
        cls.admin = User.objects.create(email="admin@negocio.com", role=cls.role)

    @classmethod
    def tearDownClass(cls):
        TenantSettings.objects.filter(tenant=cls.tenant).delete()
        super().tearDownClass()

    def test_grant_permission_creates_role_permission_and_history(self):
        from usuarios.models import RolePermissionsHistory

        RoleService.grant_permission(self.role, self.permission, changed_by=self.admin)

        self.assertTrue(
            RolePermission.objects.filter(
                role=self.role, permission=self.permission
            ).exists()
        )
        self.assertTrue(
            RolePermissionsHistory.objects.filter(
                role=self.role, permission=self.permission, action="GRANTED"
            ).exists()
        )

    def test_granting_same_permission_twice_writes_history_only_once(self):
        from usuarios.models import RolePermissionsHistory

        RoleService.grant_permission(self.role, self.permission, changed_by=self.admin)
        RoleService.grant_permission(self.role, self.permission, changed_by=self.admin)

        self.assertEqual(
            RolePermissionsHistory.objects.filter(
                role=self.role, permission=self.permission, action="GRANTED"
            ).count(),
            1,
        )

    def test_revoke_permission_removes_row_and_writes_history(self):
        from usuarios.models import RolePermissionsHistory

        RoleService.grant_permission(self.role, self.permission, changed_by=self.admin)
        RoleService.revoke_permission(self.role, self.permission, changed_by=self.admin)

        self.assertFalse(
            RolePermission.objects.filter(
                role=self.role, permission=self.permission
            ).exists()
        )
        self.assertTrue(
            RolePermissionsHistory.objects.filter(
                role=self.role, permission=self.permission, action="REVOKED"
            ).exists()
        )


class AuditLogServiceTests(TenantTestCase):
    @classmethod
    def get_test_schema_name(cls):
        return "test_audit_log_service"

    @classmethod
    def get_test_tenant_domain(cls):
        return "test-audit-log-service.test.com"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.role = Role.objects.create(name="admin")
        cls.user = User.objects.create(email="admin@negocio.com", role=cls.role)

    @classmethod
    def tearDownClass(cls):
        TenantSettings.objects.filter(tenant=cls.tenant).delete()
        super().tearDownClass()

    def test_log_action_persists_entry(self):
        AuditLogService.log_action(
            user=self.user,
            action="USER_ROLE_CHANGED",
            entity="Role",
            entity_id=self.role.id,
            details={"granted": "HR_MANAGE"},
        )

        entry = AuditLog.objects.get(user=self.user, action="USER_ROLE_CHANGED")
        self.assertEqual(entry.entity, "Role")
        self.assertIn("HR_MANAGE", entry.details)

    def test_log_action_accepts_plain_string_details(self):
        AuditLogService.log_action(
            user=self.user,
            action="USER_ROLE_CHANGED",
            entity="Role",
            entity_id=self.role.id,
            details="detalle en texto plano",
        )
        entry = AuditLog.objects.get(user=self.user, action="USER_ROLE_CHANGED")
        self.assertEqual(entry.details, "detalle en texto plano")
