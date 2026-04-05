from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from apps.core.models import User, OfficeSettings, AuditLog, CoreNotification


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    list_display = ['email', 'full_name', 'employee_id', 'status', 'is_online', 'date_joined']
    list_filter = ['status', 'is_active', 'is_staff', 'is_online']
    search_fields = ['email', 'first_name', 'last_name', 'employee_id']
    ordering = ['last_name', 'first_name']
    fieldsets = BaseUserAdmin.fieldsets + (
        ('Office Info', {'fields': ('employee_id', 'phone', 'avatar', 'status', 'date_joined_office', 'bio', 'skills')}),
        ('Preferences', {'fields': ('theme', 'notification_email', 'notification_push', 'timezone_pref', 'language')}),
    )
    add_fieldsets = BaseUserAdmin.add_fieldsets + (
        ('Office Info', {'fields': ('email', 'first_name', 'last_name', 'employee_id', 'phone')}),
    )


@admin.register(OfficeSettings)
class OfficeSettingsAdmin(admin.ModelAdmin):
    list_display = ['key', 'label', 'value', 'group', 'value_type']
    list_filter = ['group', 'value_type']
    search_fields = ['key', 'label']


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ['user', 'action', 'model_name', 'object_repr', 'timestamp']
    list_filter = ['action', 'model_name']
    readonly_fields = ['user', 'action', 'model_name', 'object_id', 'object_repr', 'changes', 'ip_address', 'timestamp']


@admin.register(CoreNotification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ['recipient', 'title', 'notification_type', 'is_read', 'created_at']
    list_filter = ['notification_type', 'is_read']
