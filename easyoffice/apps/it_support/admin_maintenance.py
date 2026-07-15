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
        'maintenance_number', 'equipment_name', 'owner_name', 'customer',
        'status', 'priority', 'assigned_to', 'promised_completion_at',
        'pickup_deadline_at', 'payment_status', 'total_amount',
    )
    list_filter = ('status', 'priority', 'payment_status', 'intake_channel')
    search_fields = (
        'maintenance_number', 'equipment_name', 'serial_number', 'asset_tag',
        'owner_name', 'owner_email', 'owner_phone',
        'customer__full_name', 'customer__company_name', 'customer__customer_code',
    )
    # raw_id_fields, not autocomplete_fields: autocomplete requires the
    # target model's ModelAdmin to declare search_fields (admin.E040), and
    # customer_service's CustomerAdmin may not. raw_id has no such coupling.
    raw_id_fields = ('customer', 'owner_user', 'assigned_to', 'ticket', 'equipment')
    list_select_related = ('customer', 'assigned_to')
    readonly_fields = (
        'maintenance_number', 'subtotal', 'tax_amount', 'total_amount',
        'created_at', 'updated_at',
    )
    inlines = [MaintenanceChargeInline, MaintenanceEventInline]


admin.site.register(MaintenanceAccessToken)
admin.site.register(MaintenanceAttachment)
admin.site.register(MaintenanceReminder)
admin.site.register(MaintenanceNotificationLog)
