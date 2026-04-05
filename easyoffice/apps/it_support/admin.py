from django.contrib import admin
from apps.it_support.models import ITRequest, Equipment


@admin.register(ITRequest)
class ITRequestAdmin(admin.ModelAdmin):
    list_display = ['ticket_number', 'title', 'requested_by', 'request_type', 'priority', 'status', 'assigned_to', 'created_at']
    list_filter = ['status', 'priority', 'request_type']
    search_fields = ['ticket_number', 'title', 'requested_by__email']


@admin.register(Equipment)
class EquipmentAdmin(admin.ModelAdmin):
    list_display = ['asset_tag', 'name', 'category', 'status', 'current_user', 'issued_date']
    list_filter = ['status', 'category']
    search_fields = ['asset_tag', 'name', 'serial_number']
