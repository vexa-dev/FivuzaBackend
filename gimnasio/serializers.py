from rest_framework import serializers

from gimnasio.models import (
    ClassBooking,
    ClassSchedule,
    GymClass,
    Membership,
    MembershipGroup,
    MembershipPayment,
    MembershipPlan,
)
from gimnasio.services import (
    ClassBookingService,
    MembershipGroupService,
    MembershipService,
)
from usuarios.models import Employee
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
            "group",
            "payments",
            "created_at",
        ]
        read_only_fields = [
            "end_date",
            "status",
            "frozen_since",
            "group",
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


class GymClassSerializer(serializers.ModelSerializer):
    instructor = serializers.PrimaryKeyRelatedField(queryset=Employee.objects.all())
    instructor_name = serializers.CharField(
        source="instructor.full_name", read_only=True
    )

    class Meta:
        model = GymClass
        fields = [
            "id",
            "name",
            "instructor",
            "instructor_name",
            "max_capacity",
            "duration_minutes",
            "is_active",
            "created_at",
        ]
        read_only_fields = ["created_at"]


class ClassScheduleSerializer(serializers.ModelSerializer):
    class Meta:
        model = ClassSchedule
        fields = ["id", "gym_class", "day_of_week", "start_time", "is_active"]


class ClassBookingSerializer(serializers.ModelSerializer):
    class Meta:
        model = ClassBooking
        fields = [
            "id",
            "customer",
            "gym_class",
            "class_date",
            "status",
            "created_at",
        ]
        read_only_fields = ["status", "created_at"]


class ClassBookingCreateSerializer(serializers.Serializer):
    customer_id = serializers.PrimaryKeyRelatedField(
        source="customer", queryset=Customer.objects.all()
    )
    gym_class_id = serializers.PrimaryKeyRelatedField(
        source="gym_class", queryset=GymClass.objects.all()
    )
    class_date = serializers.DateField()

    def create(self, validated_data):
        return ClassBookingService.book_class(
            customer=validated_data["customer"],
            gym_class=validated_data["gym_class"],
            class_date=validated_data["class_date"],
        )


class MembershipGroupSerializer(serializers.ModelSerializer):
    memberships = MembershipSerializer(many=True, read_only=True)

    class Meta:
        model = MembershipGroup
        fields = ["id", "holder_customer", "name", "memberships", "created_at"]
        read_only_fields = ["memberships", "created_at"]


class MembershipGroupCreateSerializer(serializers.Serializer):
    holder_customer_id = serializers.PrimaryKeyRelatedField(
        source="holder_customer", queryset=Customer.objects.all()
    )
    name = serializers.CharField(required=False, allow_blank=True, default="")
    membership_ids = serializers.PrimaryKeyRelatedField(
        source="memberships", queryset=Membership.objects.all(), many=True
    )

    def create(self, validated_data):
        return MembershipGroupService.create_group(
            holder_customer=validated_data["holder_customer"],
            name=validated_data.get("name", ""),
            memberships=list(validated_data["memberships"]),
        )
