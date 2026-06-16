"""
apps/mobile_api/serializers_tasks.py
────────────────────────────────────
Task serializers for the EasyOffice mobile app. Mirrors the Task model
in apps/tasks/models.py.
"""

from rest_framework import serializers
from django.utils import timezone

from apps.tasks.models import Task
from apps.mobile_api.serializers import UserMiniSerializer, _safe_full_name


class TaskListSerializer(serializers.ModelSerializer):
    """Compact shape for the open / recently-closed lists."""
    assigned_by = serializers.SerializerMethodField()
    category_name = serializers.SerializerMethodField()
    is_overdue = serializers.BooleanField(read_only=True)
    is_customer_facing = serializers.SerializerMethodField()

    class Meta:
        model = Task
        fields = [
            'id', 'title', 'priority', 'status',
            'due_date', 'completed_at', 'progress_pct',
            'on_site_at', 'awaiting_cs_verification',
            'customer_visible', 'is_overdue',
            'is_customer_facing', 'assigned_by', 'category_name',
            'created_at', 'updated_at',
        ]

    def get_assigned_by(self, obj):
        if not obj.assigned_by:
            return None
        return UserMiniSerializer(obj.assigned_by, context=self.context).data

    def get_category_name(self, obj):
        return obj.category.name if obj.category_id else ''

    def get_is_customer_facing(self, obj):
        return bool(obj.customer_visible or obj.awaiting_cs_verification)


class TaskDetailSerializer(TaskListSerializer):
    """Full shape for the detail screen."""
    assigned_to = serializers.SerializerMethodField()
    on_site_by = serializers.SerializerMethodField()
    has_activity = serializers.BooleanField(read_only=True)

    class Meta(TaskListSerializer.Meta):
        fields = TaskListSerializer.Meta.fields + [
            'description', 'assigned_to', 'on_site_by',
            'estimated_hours', 'actual_hours', 'has_activity',
            'start_date',
        ]

    def get_assigned_to(self, obj):
        if not obj.assigned_to:
            return None
        return UserMiniSerializer(obj.assigned_to, context=self.context).data

    def get_on_site_by(self, obj):
        if not obj.on_site_by_id or not obj.on_site_by:
            return None
        return UserMiniSerializer(obj.on_site_by, context=self.context).data