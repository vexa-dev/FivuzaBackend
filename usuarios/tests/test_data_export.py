# Pruebas de TenantDataExportService: respaldo completo asincrono del
# negocio (Sprint 33, Ley N 29733, API Spec §4.17). boto3 se mockea -no
# hay bucket S3 real contra el que probar en este entorno de desarrollo
# (mismo hueco documentado desde el Sprint 23 para ReportExportService).
from unittest import mock

from django.test import override_settings
from django_tenants.test.cases import TenantTestCase
from rest_framework.test import APIClient

from core.models import TenantSettings
from usuarios.models import DataExport, Role, User
from usuarios.services import DataExportLimitExceededError, TenantDataExportService


@override_settings(AWS_STORAGE_BUCKET_NAME="fivuza-test-bucket")
class TenantDataExportServiceTests(TenantTestCase):
    @classmethod
    def get_test_schema_name(cls):
        return "test_data_export_service"

    @classmethod
    def get_test_tenant_domain(cls):
        return "test-data-export-service.test.com"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.role = Role.objects.get(name="admin")
        cls.user = User.objects.create(email="admin@negocio.com", role=cls.role)

    @classmethod
    def tearDownClass(cls):
        TenantSettings.objects.filter(tenant=cls.tenant).delete()
        super().tearDownClass()

    @mock.patch("usuarios.tasks.boto3.client")
    def test_request_export_completes_and_uploads_to_s3(self, mock_boto_client):
        export = TenantDataExportService.request_export(user=self.user, format="ZIP")
        export.refresh_from_db()

        self.assertEqual(export.status, "COMPLETED")
        self.assertTrue(export.file_key.endswith(f"{export.id}.zip"))
        self.assertIsNotNone(export.expires_at)
        mock_boto_client.return_value.put_object.assert_called_once()

    @mock.patch("usuarios.tasks.boto3.client")
    def test_request_export_xlsx_format(self, mock_boto_client):
        export = TenantDataExportService.request_export(user=self.user, format="XLSX")
        export.refresh_from_db()
        self.assertTrue(export.file_key.endswith(".xlsx"))

    @mock.patch("usuarios.tasks.boto3.client")
    def test_second_export_same_day_is_rejected(self, mock_boto_client):
        TenantDataExportService.request_export(user=self.user, format="ZIP")
        with self.assertRaises(DataExportLimitExceededError):
            TenantDataExportService.request_export(user=self.user, format="ZIP")

    def test_build_export_file_excludes_password_field(self):
        content, extension = TenantDataExportService.build_export_file(format="ZIP")
        self.assertEqual(extension, "zip")
        import io
        import zipfile

        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            users_csv = archive.read("users.csv").decode()
        self.assertNotIn("password", users_csv.lower())

    @mock.patch("usuarios.services.boto3.client")
    def test_get_download_url_generates_presigned_url_for_completed_export(
        self, mock_boto_client
    ):
        mock_boto_client.return_value.generate_presigned_url.return_value = (
            "https://fivuza-test-bucket.s3.amazonaws.com/signed"
        )
        export = DataExport.objects.create(
            requested_by=self.user,
            format="ZIP",
            status="COMPLETED",
            file_key="tenant-exports/test/1.zip",
        )
        from django.utils import timezone
        from datetime import timedelta

        export.expires_at = timezone.now() + timedelta(hours=1)
        export.save()

        url = TenantDataExportService.get_download_url(export)
        self.assertEqual(url, "https://fivuza-test-bucket.s3.amazonaws.com/signed")

    def test_get_download_url_rejects_export_not_yet_completed(self):
        export = DataExport.objects.create(
            requested_by=self.user, format="ZIP", status="PENDING"
        )
        from usuarios.services import DataExportNotReadyError

        with self.assertRaises(DataExportNotReadyError):
            TenantDataExportService.get_download_url(export)

    def test_get_download_url_rejects_expired_export(self):
        from datetime import timedelta

        from django.utils import timezone

        export = DataExport.objects.create(
            requested_by=self.user,
            format="ZIP",
            status="COMPLETED",
            file_key="tenant-exports/test/1.zip",
            expires_at=timezone.now() - timedelta(hours=1),
        )
        from usuarios.services import DataExportExpiredError

        with self.assertRaises(DataExportExpiredError):
            TenantDataExportService.get_download_url(export)


@override_settings(AWS_STORAGE_BUCKET_NAME="fivuza-test-bucket")
class DataExportEndpointsTests(TenantTestCase):
    @classmethod
    def get_test_schema_name(cls):
        return "test_data_export_views"

    @classmethod
    def get_test_tenant_domain(cls):
        return "test-data-export-views.test.com"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.password = "ClaveSegura123"
        cls.admin_user = User.objects.create(
            email="admin@negocio.com", role=Role.objects.get(name="admin")
        )
        cls.admin_user.set_password(cls.password)
        cls.admin_user.save()
        cls.seller_user = User.objects.create(
            email="vendedor@negocio.com", role=Role.objects.get(name="seller")
        )
        cls.seller_user.set_password(cls.password)
        cls.seller_user.save()

    @classmethod
    def tearDownClass(cls):
        TenantSettings.objects.filter(tenant=cls.tenant).delete()
        super().tearDownClass()

    def _client_as(self, user):
        client = APIClient(HTTP_HOST=self.domain.domain)
        login = client.post(
            "/api/v1/auth/login/",
            {"email": user.email, "password": self.password},
            format="json",
        )
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {login.data['access']}")
        return client

    @mock.patch("usuarios.tasks.boto3.client")
    def test_admin_can_request_and_list_exports(self, mock_boto_client):
        client = self._client_as(self.admin_user)
        response = client.post(
            "/api/v1/usuarios/data-exports/", {"format": "ZIP"}, format="json"
        )
        self.assertEqual(response.status_code, 201)

        listing = client.get("/api/v1/usuarios/data-exports/")
        self.assertEqual(listing.status_code, 200)
        self.assertEqual(len(listing.data), 1)

    def test_seller_without_data_export_permission_is_rejected(self):
        client = self._client_as(self.seller_user)
        response = client.post(
            "/api/v1/usuarios/data-exports/", {"format": "ZIP"}, format="json"
        )
        self.assertEqual(response.status_code, 403)

    @mock.patch("usuarios.services.boto3.client")
    @mock.patch("usuarios.tasks.boto3.client")
    def test_download_endpoint_returns_presigned_url(
        self, mock_task_boto, mock_service_boto
    ):
        mock_service_boto.return_value.generate_presigned_url.return_value = (
            "https://fivuza-test-bucket.s3.amazonaws.com/signed"
        )
        client = self._client_as(self.admin_user)
        create_response = client.post(
            "/api/v1/usuarios/data-exports/", {"format": "ZIP"}, format="json"
        )
        export_id = create_response.data["id"]

        response = client.get(f"/api/v1/usuarios/data-exports/{export_id}/download/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("download_url", response.data)
