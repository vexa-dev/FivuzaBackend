from django.db import models


class Role(models.Model):
    name = models.CharField(max_length=50)
    is_system_default = models.BooleanField(default=False)
    description = models.CharField(max_length=255, blank=True)

    class Meta:
        db_table = "roles"

    def __str__(self):
        return self.name


class Permission(models.Model):
    code = models.CharField(max_length=100, unique=True)
    module = models.CharField(
        max_length=20,
        choices=[
            ("SALES", "SALES"),
            ("INVENTORY", "INVENTORY"),
            ("PURCHASES", "PURCHASES"),
            ("USERS", "USERS"),
            ("REPORTS", "REPORTS"),
            ("HR", "HR"),
            ("CASH", "CASH"),
        ],
    )
    description = models.CharField(max_length=255, blank=True)

    class Meta:
        db_table = "permissions"

    def __str__(self):
        return self.code


class RolePermission(models.Model):
    role = models.ForeignKey(Role, on_delete=models.CASCADE, related_name="role_permissions")
    permission = models.ForeignKey(
        Permission, on_delete=models.CASCADE, related_name="role_permissions"
    )

    class Meta:
        db_table = "role_permissions"
        constraints = [
            models.UniqueConstraint(
                fields=["role", "permission"], name="uq_role_permission"
            )
        ]


class User(models.Model):
    email = models.EmailField()
    password = models.CharField(max_length=255)
    role = models.ForeignKey(Role, on_delete=models.PROTECT, related_name="users")
    is_active = models.BooleanField(default=True)
    last_login = models.DateTimeField(null=True, blank=True)
    deleted_at = models.DateTimeField(null=True, blank=True)
    deleted_by = models.ForeignKey(
        "self", on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "users"
        constraints = [
            models.UniqueConstraint(fields=["email"], name="uq_users_email"),
        ]

    def __str__(self):
        return self.email


class RolePermissionsHistory(models.Model):
    """MEJORA 5: historial inmutable de otorgamiento/revocación de permisos por rol."""

    role = models.ForeignKey(Role, on_delete=models.PROTECT, related_name="permission_history")
    permission = models.ForeignKey(
        Permission, on_delete=models.PROTECT, related_name="role_history"
    )
    action = models.CharField(
        max_length=10, choices=[("GRANTED", "GRANTED"), ("REVOKED", "REVOKED")]
    )
    changed_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name="+")
    changed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "role_permissions_history"
        constraints = [
            models.CheckConstraint(
                check=models.Q(action__in=["GRANTED", "REVOKED"]),
                name="ck_role_permissions_history_action",
            )
        ]


class UserPermission(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="permission_overrides")
    permission = models.ForeignKey(Permission, on_delete=models.CASCADE, related_name="+")
    is_granted = models.BooleanField()

    class Meta:
        db_table = "user_permissions"


class UserWarehouse(models.Model):
    """MEJORA 7: sucursales a las que puede operar cada usuario."""

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="warehouse_access")
    warehouse = models.ForeignKey(
        "inventario.Warehouse", on_delete=models.CASCADE, related_name="+"
    )

    class Meta:
        db_table = "user_warehouses"
        constraints = [
            models.UniqueConstraint(
                fields=["user", "warehouse"], name="uq_user_warehouse"
            )
        ]


class AuditLog(models.Model):
    """Particionada nativamente por RANGE sobre created_at (mensual) a nivel de DB; el modelo
    Django gestiona la tabla base, el particionado se aplica con una migración manual de SQL."""

    user = models.ForeignKey(User, on_delete=models.PROTECT, related_name="audit_logs")
    action = models.CharField(max_length=100)
    entity = models.CharField(max_length=100)
    entity_id = models.IntegerField()
    details = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "audit_logs"


class Employee(models.Model):
    """Ficha de trabajador, separada del concepto de User (acceso al sistema es opcional)."""

    user = models.OneToOneField(
        User, on_delete=models.PROTECT, null=True, blank=True, related_name="employee"
    )
    full_name = models.CharField(max_length=200)
    document_number = models.CharField(max_length=20, unique=True)
    phone = models.CharField(max_length=30, blank=True)
    position = models.CharField(max_length=100)
    warehouse = models.ForeignKey(
        "inventario.Warehouse", on_delete=models.PROTECT, related_name="employees"
    )
    salary_type = models.CharField(
        max_length=10,
        choices=[("MONTHLY", "MONTHLY"), ("DAILY", "DAILY"), ("HOURLY", "HOURLY")],
    )
    salary_amount = models.DecimalField(max_digits=12, decimal_places=4)
    currency = models.CharField(max_length=3, default="PEN")
    hire_date = models.DateField()
    termination_date = models.DateField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    deleted_at = models.DateTimeField(null=True, blank=True)
    deleted_by = models.ForeignKey(
        User, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "employees"
        constraints = [
            models.CheckConstraint(
                check=models.Q(salary_type__in=["MONTHLY", "DAILY", "HOURLY"]),
                name="ck_employees_salary_type",
            )
        ]

    def __str__(self):
        return self.full_name


class EmployeeSchedule(models.Model):
    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name="schedules")
    day_of_week = models.CharField(
        max_length=10,
        choices=[
            ("MONDAY", "MONDAY"),
            ("TUESDAY", "TUESDAY"),
            ("WEDNESDAY", "WEDNESDAY"),
            ("THURSDAY", "THURSDAY"),
            ("FRIDAY", "FRIDAY"),
            ("SATURDAY", "SATURDAY"),
            ("SUNDAY", "SUNDAY"),
        ],
    )
    start_time = models.TimeField()
    end_time = models.TimeField()
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "employee_schedules"
        constraints = [
            models.CheckConstraint(
                check=models.Q(
                    day_of_week__in=[
                        "MONDAY",
                        "TUESDAY",
                        "WEDNESDAY",
                        "THURSDAY",
                        "FRIDAY",
                        "SATURDAY",
                        "SUNDAY",
                    ]
                ),
                name="ck_employee_schedules_day",
            )
        ]


class EmployeeAttendance(models.Model):
    employee = models.ForeignKey(Employee, on_delete=models.PROTECT, related_name="attendance")
    warehouse = models.ForeignKey(
        "inventario.Warehouse", on_delete=models.PROTECT, related_name="+"
    )
    check_in = models.DateTimeField()
    check_out = models.DateTimeField(null=True, blank=True)
    status = models.CharField(
        max_length=25,
        choices=[
            ("ON_TIME", "ON_TIME"),
            ("LATE", "LATE"),
            ("ABSENCE_JUSTIFIED", "ABSENCE_JUSTIFIED"),
            ("ABSENCE_UNJUSTIFIED", "ABSENCE_UNJUSTIFIED"),
        ],
    )
    notes = models.CharField(max_length=255, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "employee_attendance"
        verbose_name_plural = "employee attendance"
        constraints = [
            models.CheckConstraint(
                check=models.Q(
                    status__in=[
                        "ON_TIME",
                        "LATE",
                        "ABSENCE_JUSTIFIED",
                        "ABSENCE_UNJUSTIFIED",
                    ]
                ),
                name="ck_employee_attendance_status",
            )
        ]


class EmployeePayroll(models.Model):
    employee = models.ForeignKey(Employee, on_delete=models.PROTECT, related_name="payroll")
    period_start = models.DateField()
    period_end = models.DateField()
    base_salary = models.DecimalField(max_digits=12, decimal_places=4)
    bonuses = models.DecimalField(max_digits=12, decimal_places=4, default=0)
    deductions = models.DecimalField(max_digits=12, decimal_places=4, default=0)
    net_amount = models.DecimalField(max_digits=12, decimal_places=4)
    status = models.CharField(
        max_length=10, choices=[("PENDING", "PENDING"), ("PAID", "PAID")]
    )
    payment_date = models.DateField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "employee_payroll"
        constraints = [
            models.CheckConstraint(
                check=models.Q(status__in=["PENDING", "PAID"]),
                name="ck_employee_payroll_status",
            )
        ]
