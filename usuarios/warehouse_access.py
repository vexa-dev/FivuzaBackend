"""Autorización centralizada de acceso a almacenes dentro de un tenant."""

from rest_framework.exceptions import PermissionDenied


class WarehouseAccessDenied(PermissionDenied):
    default_code = "WAREHOUSE_ACCESS_DENIED"
    default_detail = {
        "error": {
            "code": "WAREHOUSE_ACCESS_DENIED",
            "message": "No tienes acceso al almacén solicitado.",
            "details": {},
        }
    }


class WarehouseAccessService:
    """Único punto de verdad para limitar datos y operaciones por almacén."""

    @staticmethod
    def is_admin(user) -> bool:
        role = getattr(user, "role", None)
        return bool(role and role.is_system_default and role.name.casefold() == "admin")

    @staticmethod
    def allowed_warehouse_ids(user) -> tuple[int, ...]:
        from inventario.models import Warehouse
        from usuarios.models import UserWarehouse

        if WarehouseAccessService.is_admin(user):
            return tuple(
                Warehouse.objects.filter(is_active=True)
                .order_by("id")
                .values_list("id", flat=True)
            )
        return tuple(
            UserWarehouse.objects.filter(user=user, warehouse__is_active=True)
            .order_by("warehouse_id")
            .values_list("warehouse_id", flat=True)
        )

    @staticmethod
    def scope_queryset(queryset, user, lookup: str = "warehouse_id"):
        if WarehouseAccessService.is_admin(user):
            return queryset
        return queryset.filter(
            **{f"{lookup}__in": WarehouseAccessService.allowed_warehouse_ids(user)}
        )

    @staticmethod
    def require_warehouse(user, warehouse_or_id):
        warehouse_id = getattr(warehouse_or_id, "pk", warehouse_or_id)
        if warehouse_id is None:
            raise WarehouseAccessDenied()
        if WarehouseAccessService.is_admin(user):
            return warehouse_or_id
        if int(warehouse_id) not in WarehouseAccessService.allowed_warehouse_ids(user):
            raise WarehouseAccessDenied()
        return warehouse_or_id

    @staticmethod
    def allowed_warehouses(user):
        from inventario.models import Warehouse

        return WarehouseAccessService.scope_queryset(
            Warehouse.objects.filter(is_active=True), user, lookup="id"
        )
