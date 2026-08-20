from django.conf import settings


def _cookie_name(platform: bool) -> str:
    return (
        settings.PLATFORM_REFRESH_COOKIE_NAME
        if platform
        else settings.TENANT_REFRESH_COOKIE_NAME
    )


def _cookie_path(platform: bool) -> str:
    return "/api/v1/platform/auth/" if platform else "/api/v1/auth/"


def get_refresh_cookie(request, *, platform: bool = False) -> str | None:
    return request.COOKIES.get(_cookie_name(platform))


def set_refresh_cookie(response, token: str, *, platform: bool = False) -> None:
    response.set_cookie(
        _cookie_name(platform),
        token,
        max_age=int(settings.SIMPLE_JWT["REFRESH_TOKEN_LIFETIME"].total_seconds()),
        path=_cookie_path(platform),
        secure=settings.AUTH_COOKIE_SECURE,
        httponly=True,
        samesite="Lax",
    )


def clear_refresh_cookie(response, *, platform: bool = False) -> None:
    response.delete_cookie(
        _cookie_name(platform), path=_cookie_path(platform), samesite="Lax"
    )
