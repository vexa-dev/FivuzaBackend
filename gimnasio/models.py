from django.db import models

from usuarios.models import User
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
