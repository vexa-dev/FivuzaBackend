from drf_spectacular.extensions import OpenApiAuthenticationExtension


class TenantJWTAuthenticationScheme(OpenApiAuthenticationExtension):
    target_class = "core.authentication.TenantValidatedJWTAuthentication"
    name = "tenantBearerAuth"

    def get_security_definition(self, auto_schema):
        return {"type": "http", "scheme": "bearer", "bearerFormat": "JWT"}


class PlatformJWTAuthenticationScheme(OpenApiAuthenticationExtension):
    target_class = "core.authentication.PlatformStaffJWTAuthentication"
    name = "platformBearerAuth"

    def get_security_definition(self, auto_schema):
        return {"type": "http", "scheme": "bearer", "bearerFormat": "JWT"}
