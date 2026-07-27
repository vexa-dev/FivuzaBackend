# Pruebas de ViewSets/vistas: permisos, serialización, códigos de respuesta HTTP.
from django_tenants.test.cases import TenantTestCase
from rest_framework.test import APIClient

from core.models import TenantSettings
from usuarios.models import Permission, Role, User


class TenantUserAuthTests(TenantTestCase):
    """Login/refresh/logout de tenant.users (API Spec §3.1, Sprint 2)."""

    @classmethod
    def get_test_schema_name(cls):
        return "test_usuarios_auth"

    @classmethod
    def get_test_tenant_domain(cls):
        return "test-usuarios-auth.test.com"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.role = Role.objects.create(name="admin", is_system_default=True)
        cls.password = "ClaveSegura123"
        cls.user = User.objects.create(email="admin@negocio.com", role=cls.role)
        cls.user.set_password(cls.password)
        cls.user.save()

    @classmethod
    def tearDownClass(cls):
        TenantSettings.objects.filter(tenant=cls.tenant).delete()
        super().tearDownClass()

    def setUp(self):
        self.client = APIClient(HTTP_HOST=self.domain.domain)

    def _login(self):
        return self.client.post(
            "/api/v1/auth/login/",
            {"email": self.user.email, "password": self.password},
            format="json",
        )

    def test_login_with_valid_credentials_returns_tokens(self):
        response = self._login()
        self.assertEqual(response.status_code, 200)
        self.assertIn("access", response.data)
        self.assertIn("refresh", response.data)
        self.assertEqual(response.data["user"]["email"], self.user.email)

    def test_login_with_wrong_password_fails(self):
        response = self.client.post(
            "/api/v1/auth/login/",
            {"email": self.user.email, "password": "incorrecta"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)

    def test_login_with_inactive_user_fails(self):
        self.user.is_active = False
        self.user.save()
        response = self._login()
        self.assertEqual(response.status_code, 400)
        self.user.is_active = True
        self.user.save()

    def test_logout_requires_authentication(self):
        response = self.client.post(
            "/api/v1/auth/logout/", {"refresh": "x"}, format="json"
        )
        self.assertEqual(response.status_code, 401)

    def test_refresh_then_logout_blacklists_refresh_token(self):
        tokens = self._login().data
        access, refresh = tokens["access"], tokens["refresh"]

        refresh_response = self.client.post(
            "/api/v1/auth/refresh/", {"refresh": refresh}, format="json"
        )
        self.assertEqual(refresh_response.status_code, 200)

        old_refresh_reuse = self.client.post(
            "/api/v1/auth/refresh/", {"refresh": refresh}, format="json"
        )
        self.assertEqual(old_refresh_reuse.status_code, 401)

        new_refresh = refresh_response.data["refresh"]
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
        logout_response = self.client.post(
            "/api/v1/auth/logout/", {"refresh": new_refresh}, format="json"
        )
        self.assertEqual(logout_response.status_code, 205)

        reuse_after_logout = self.client.post(
            "/api/v1/auth/refresh/", {"refresh": new_refresh}, format="json"
        )
        self.assertEqual(reuse_after_logout.status_code, 401)


class RoleRBACEndpointsTests(TenantTestCase):
    """CRUD de roles/permisos/usuarios y aplicacion de HasModulePermission
    (API Spec §2.1, Sprint 2)."""

    @classmethod
    def get_test_schema_name(cls):
        return "test_usuarios_rbac"

    @classmethod
    def get_test_tenant_domain(cls):
        return "test-usuarios-rbac.test.com"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # TenantProvisioningService.seed_default_roles() (post_schema_sync)
        # ya creo los roles admin/manager/seller y el catalogo base de
        # permisos al crear el tenant -se reutilizan aqui en vez de crear
        # duplicados con el mismo permissions.code (unique).
        cls.password = "ClaveSegura123"
        cls.admin_role = Role.objects.get(name="admin")
        cls.seller_role = Role.objects.get(name="seller")
        cls.manage_users_perm = Permission.objects.get(code="USERS_MANAGE")

        cls.admin_user = User.objects.create(
            email="admin@negocio.com", role=cls.admin_role
        )
        cls.admin_user.set_password(cls.password)
        cls.admin_user.save()

        cls.seller_user = User.objects.create(
            email="vendedor@negocio.com", role=cls.seller_role
        )
        cls.seller_user.set_password(cls.password)
        cls.seller_user.save()

    @classmethod
    def tearDownClass(cls):
        TenantSettings.objects.filter(tenant=cls.tenant).delete()
        super().tearDownClass()

    def setUp(self):
        # Ver PermissionServiceTests.setUp: la cache de permisos vive en
        # Redis, fuera de la transaccion de BD que Django revierte por test.
        from django.core.cache import cache

        cache.clear()

    def _client_as(self, user):
        client = APIClient(HTTP_HOST=self.domain.domain)
        login = client.post(
            "/api/v1/auth/login/",
            {"email": user.email, "password": self.password},
            format="json",
        )
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {login.data['access']}")
        return client

    def test_seller_without_permission_cannot_manage_users(self):
        response = self._client_as(self.seller_user).get("/api/v1/usuarios/users/")
        self.assertEqual(response.status_code, 403)

    def test_admin_with_permission_can_manage_users(self):
        response = self._client_as(self.admin_user).get("/api/v1/usuarios/users/")
        self.assertEqual(response.status_code, 200)

    def test_admin_can_create_user_with_hashed_password(self):
        response = self._client_as(self.admin_user).post(
            "/api/v1/usuarios/users/",
            {
                "email": "nuevo@negocio.com",
                "role": self.seller_role.id,
                "password": "OtraClave456",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        self.assertNotIn("password", response.data)

        created = User.objects.get(email="nuevo@negocio.com")
        self.assertNotEqual(created.password, "OtraClave456")
        self.assertTrue(created.check_password("OtraClave456"))

    def test_deleting_user_soft_deletes_and_excludes_from_default_manager(self):
        target = User.objects.create(email="baja@negocio.com", role=self.seller_role)
        response = self._client_as(self.admin_user).delete(
            f"/api/v1/usuarios/users/{target.id}/"
        )
        self.assertEqual(response.status_code, 204)

        self.assertFalse(User.objects.filter(id=target.id).exists())
        self.assertTrue(User.all_objects.filter(id=target.id).exists())

    def test_permissions_catalog_is_read_only(self):
        response = self._client_as(self.admin_user).post(
            "/api/v1/usuarios/permissions/",
            {"code": "FAKE_PERM", "module": "USERS"},
            format="json",
        )
        self.assertEqual(response.status_code, 405)

    def test_granting_role_permission_writes_history(self):
        from usuarios.models import RolePermissionsHistory

        response = self._client_as(self.admin_user).post(
            "/api/v1/usuarios/role-permissions/",
            {"role": self.seller_role.id, "permission": self.manage_users_perm.id},
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        self.assertTrue(
            RolePermissionsHistory.objects.filter(
                role=self.seller_role,
                permission=self.manage_users_perm,
                action="GRANTED",
            ).exists()
        )
