"""Import these registrations from apps/it_support/admin.py."""
from django.contrib import admin

from .models import (
    MaintenanceAccessToken,
    MaintenanceAttachment,
    MaintenanceChargeLine,
    MaintenanceEvent,
    MaintenanceJob,
    MaintenanceNotificationLog,
    MaintenanceReminder,
)


class MaintenanceChargeInline(admin.TabularInline):
    model = MaintenanceChargeLine
    extra = 0


class MaintenanceEventInline(admin.TabularInline):
    model = MaintenanceEvent
    extra = 0
    readonly_fields = ('kind', 'actor', 'message', 'is_public', 'created_at')
    can_delete = False


@admin.register(MaintenanceJob)
class MaintenanceJobAdmin(admin.ModelAdmin):
    list_display = (
        'maintenance_number', 'equipment_name', 'owner_name', 'status',
        'priority', 'assigned_to', 'promised_completion_at',
        'pickup_deadline_at', 'payment_status', 'total_amount',
    )
    list_filter = ('status', 'priority', 'payment_status', 'intake_channel')
    search_fields = (
        'maintenance_number', 'equipment_name', 'serial_number', 'asset_tag',
        'owner_name', 'owner_email', 'owner_phone',
    )
    readonly_fields = (
        'maintenance_number', 'subtotal', 'tax_amount', 'total_amount',
        'created_at', 'updated_at',
    )
    inlines = [MaintenanceChargeInline, MaintenanceEventInline]


admin.site.register(MaintenanceAccessToken)
admin.site.register(MaintenanceAttachment)
admin.site.register(MaintenanceReminder)
admin.site.register(MaintenanceNotificationLog)
