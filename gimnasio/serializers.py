from rest_framework import serializers

from gimnasio.models import Membership, MembershipPayment, MembershipPlan
from gimnasio.services import MembershipService
from ventas.models import Customer


class MembershipPlanSerializer(serializers.ModelSerializer):
    class Meta:
        model = MembershipPlan
        fields = [
            "id",
            "name",
            "price",
            "periodicity",
            "benefits",
            "is_active",
            "created_at",
        ]
        read_only_fields = ["created_at"]


class MembershipPaymentSerializer(serializers.ModelSerializer):
    class Meta:
        model = MembershipPayment
        fields = ["id", "membership", "amount", "method", "user", "created_at"]
        read_only_fields = fields


class MembershipSerializer(serializers.ModelSerializer):
    payments = MembershipPaymentSerializer(many=True, read_only=True)

    class Meta:
        model = Membership
        fields = [
            "id",
            "customer",
            "plan",
            "start_date",
            "end_date",
            "status",
            "frozen_since",
            "payments",
            "created_at",
        ]
        read_only_fields = [
            "end_date",
            "status",
            "frozen_since",
            "payments",
            "created_at",
        ]


class MembershipCreateSerializer(serializers.Serializer):
    """No es un ModelSerializer -delega en MembershipService.
    create_membership(), que calcula end_date sumando la periodicidad del
    plan (mismo patron que SaleCreateSerializer)."""

    customer_id = serializers.PrimaryKeyRelatedField(
        source="customer", queryset=Customer.objects.all()
    )
    plan_id = serializers.PrimaryKeyRelatedField(
        source="plan", queryset=MembershipPlan.objects.all()
    )
    start_date = serializers.DateField()

    def create(self, validated_data):
        return MembershipService.create_membership(
            customer=validated_data["customer"],
            plan=validated_data["plan"],
            start_date=validated_data["start_date"],
            user=self.context["request"].user,
        )


class MembershipRenewSerializer(serializers.Serializer):
    payment_amount = serializers.DecimalField(
        max_digits=12, decimal_places=4, min_value=0, required=False, allow_null=True
    )
    payment_method = serializers.ChoiceField(
        choices=["CASH", "CARD", "YAPE"], required=False, default="CASH"
    )

    def create(self, validated_data):
        return MembershipService.renew_membership(
            membership=self.context["membership"],
            user=self.context["request"].user,
            payment_amount=validated_data.get("payment_amount"),
            payment_method=validated_data.get("payment_method", "CASH"),
        )
