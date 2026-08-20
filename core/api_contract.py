from django.conf import settings
from rest_framework.pagination import PageNumberPagination
from rest_framework.views import exception_handler as drf_exception_handler


class TransitionPageNumberPagination(PageNumberPagination):
    page_size = 100
    page_size_query_param = "page_size"
    max_page_size = 100

    def paginate_queryset(self, queryset, request, view=None):
        if not settings.API_V1_PAGINATION_ENABLED:
            return None
        return super().paginate_queryset(queryset, request, view)


def standard_exception_handler(exc, context):
    response = drf_exception_handler(exc, context)
    if response is None or not settings.API_STANDARD_ERRORS_ENABLED:
        return response
    if isinstance(response.data, dict) and "error" in response.data:
        return response

    status_codes = {
        400: "VALIDATION_ERROR",
        401: "AUTHENTICATION_FAILED",
        403: "PERMISSION_DENIED",
        404: "NOT_FOUND",
        405: "METHOD_NOT_ALLOWED",
        409: "CONFLICT",
        429: "THROTTLED",
    }
    code = status_codes.get(response.status_code, "API_ERROR")
    details = response.data
    message = "La solicitud no pudo procesarse."
    if isinstance(details, dict) and isinstance(details.get("detail"), str):
        message = details["detail"]
    elif isinstance(details, list) and details and isinstance(details[0], str):
        message = details[0]
    response.data = {"error": {"code": code, "message": message, "details": details}}
    return response
