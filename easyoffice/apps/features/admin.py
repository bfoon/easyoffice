from django.contrib import admin

from apps.features.models import FeatureAuditLog, FeatureFlag


@admin.register(FeatureFlag)
class FeatureFlagAdmin(admin.ModelAdmin):
    list_display = ('key', 'mode', 'superuser_preview', 'updated_by', 'updated_at')
    list_filter = ('mode',)
    filter_horizontal = ('pilot_users', 'pilot_groups')
    search_fields = ('key',)


@admin.register(FeatureAuditLog)
class FeatureAuditLogAdmin(admin.ModelAdmin):
    list_display = ('created_at', 'key', 'old_mode', 'new_mode', 'actor', 'ip_address')
    list_filter = ('key', 'new_mode')
    readonly_fields = [f.name for f in FeatureAuditLog._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
