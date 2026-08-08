from rest_framework import serializers

from dashboard.models import DashboardWidget


class DashboardWidgetSerializer(serializers.ModelSerializer):
    class Meta:
        model = DashboardWidget
        fields = ["id", "user", "widget_code", "position", "is_visible"]
        read_only_fields = ["user"]
