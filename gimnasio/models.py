from django.db import models

from usuarios.models import Employee, User
from ventas.models import Customer


class MembershipPlan(models.Model):
    """Plan de membresia del gimnasio (Sprint 29, Ficha de Producto §5.1,
    vertical de Gimnasios). El cobro real al socio (MembershipPayment) es
    un registro simple, no pasa por SaleService -es cobro del negocio a su
    cliente final, conceptualmente igual a Subscription/SubscriptionPayment
    de `core` (Fivuza cobrando al tenant), pero en el esquema del tenant."""

    name = models.CharField(max_length=150)
    price = models.DecimalField(max_digits=12, decimal_places=4)
    periodicity = models.CharField(
        max_length=10,
        choices=[
            ("MONTHLY", "MONTHLY"),
            ("QUARTERLY", "QUARTERLY"),
            ("YEARLY", "YEARLY"),
        ],
    )
    benefits = models.TextField(blank=True, default="")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "membership_plans"
        constraints = [
            models.CheckConstraint(
                check=models.Q(periodicity__in=["MONTHLY", "QUARTERLY", "YEARLY"]),
                name="ck_membership_plans_periodicity",
            )
        ]

    def __str__(self):
        return self.name


class Membership(models.Model):
    """frozen_since registra desde cuando esta congelada -al descongelar,
    MembershipService.unfreeze_membership() extiende end_date exactamente
    esos dias de pausa, para que una membresia congelada nunca "pierda"
    tiempo pagado (Ficha de Producto §5.1: "sin perder su fecha de fin real")."""

    customer = models.ForeignKey(
        Customer, on_delete=models.PROTECT, related_name="memberships"
    )
    plan = models.ForeignKey(
        MembershipPlan, on_delete=models.PROTECT, related_name="memberships"
    )
    start_date = models.DateField()
    end_date = models.DateField()
    status = models.CharField(
        max_length=10,
        choices=[
            ("ACTIVE", "ACTIVE"),
            ("FROZEN", "FROZEN"),
            ("EXPIRED", "EXPIRED"),
            ("CANCELLED", "CANCELLED"),
        ],
        default="ACTIVE",
    )
    frozen_since = models.DateField(null=True, blank=True)
    group = models.ForeignKey(
        "MembershipGroup",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="memberships",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "memberships"
        indexes = [models.Index(fields=["status", "end_date"])]
        constraints = [
            models.CheckConstraint(
                check=models.Q(status__in=["ACTIVE", "FROZEN", "EXPIRED", "CANCELLED"]),
                name="ck_memberships_status",
            )
        ]


class MembershipPayment(models.Model):
    membership = models.ForeignKey(
        Membership, on_delete=models.CASCADE, related_name="payments"
    )
    amount = models.DecimalField(max_digits=12, decimal_places=4)
    method = models.CharField(
        max_length=10,
        choices=[("CASH", "CASH"), ("CARD", "CARD"), ("YAPE", "YAPE")],
    )
    user = models.ForeignKey(
        User, on_delete=models.PROTECT, related_name="membership_payments"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "membership_payments"
        constraints = [
            models.CheckConstraint(
                check=models.Q(method__in=["CASH", "CARD", "YAPE"]),
                name="ck_membership_payments_method",
            )
        ]


class MembershipGroup(models.Model):
    """Membresia familiar/grupal (Sprint 30): agrupa 2+ Membership bajo un
    solo titular de pago. El titular no necesariamente tiene una Membership
    propia dentro del grupo -puede ser quien paga por su familia sin
    entrenar el mismo."""

    holder_customer = models.ForeignKey(
        Customer, on_delete=models.PROTECT, related_name="membership_groups_held"
    )
    name = models.CharField(max_length=150, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "membership_groups"

    def __str__(self):
        return self.name or f"Grupo #{self.pk}"


class GymClass(models.Model):
    name = models.CharField(max_length=150)
    instructor = models.ForeignKey(
        Employee, on_delete=models.PROTECT, related_name="gym_classes"
    )
    max_capacity = models.PositiveIntegerField()
    duration_minutes = models.PositiveIntegerField()
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "gym_classes"

    def __str__(self):
        return self.name


class ClassSchedule(models.Model):
    gym_class = models.ForeignKey(
        GymClass, on_delete=models.CASCADE, related_name="schedules"
    )
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
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "class_schedules"
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
                name="ck_class_schedules_day",
            )
        ]


class ClassBooking(models.Model):
    """La reserva se identifica por clase+fecha (no por una ClassSchedule
    puntual): el cupo maximo de una sesion dada es el de su GymClass, y
    ClassBookingService.book_class() cuenta las reservas RESERVADO/ASISTIO
    de esa combinacion para decidir si hay cupo disponible."""

    customer = models.ForeignKey(
        Customer, on_delete=models.PROTECT, related_name="class_bookings"
    )
    gym_class = models.ForeignKey(
        GymClass, on_delete=models.PROTECT, related_name="bookings"
    )
    class_date = models.DateField()
    status = models.CharField(
        max_length=10,
        choices=[
            ("RESERVADO", "RESERVADO"),
            ("ASISTIO", "ASISTIO"),
            ("NO_ASISTIO", "NO_ASISTIO"),
            ("CANCELADO", "CANCELADO"),
        ],
        default="RESERVADO",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "class_bookings"
        indexes = [models.Index(fields=["gym_class", "class_date", "status"])]
        constraints = [
            models.CheckConstraint(
                check=models.Q(
                    status__in=["RESERVADO", "ASISTIO", "NO_ASISTIO", "CANCELADO"]
                ),
                name="ck_class_bookings_status",
            )
        ]
