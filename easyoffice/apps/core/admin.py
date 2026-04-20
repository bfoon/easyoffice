from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.utils.html import format_html
from apps.core.models import (
    User, OfficeSettings, AuditLog, CoreNotification,
    TrustedDevice, OTPChallenge, DeviceSwitchToken,
    SecuritySettings, SecurityEvent,
)


# ─────────────────────────────────────────────────────────────────────────────
# User
# ─────────────────────────────────────────────────────────────────────────────

@admin.register(User)
class UserAdmin(BaseUserAdmin):
    list_display  = ['email', 'full_name', 'employee_id', 'status',
                     'is_online', 'failed_login_count', 'lockout_status', 'date_joined']
    list_filter   = ['status', 'is_active', 'is_staff', 'is_online']
    search_fields = ['email', 'first_name', 'last_name', 'employee_id']
    ordering      = ['last_name', 'first_name']
    readonly_fields = ['last_seen', 'is_online', 'failed_login_count',
                       'lockout_until', 'last_login_ip', 'last_login_city',
                       'last_login_country', 'active_device']

    fieldsets = BaseUserAdmin.fieldsets + (
        ('Office Info', {
            'fields': ('employee_id', 'phone', 'avatar', 'status',
                       'date_joined_office', 'bio', 'skills'),
        }),
        ('Preferences', {
            'fields': ('theme', 'notification_email', 'notification_push',
                       'timezone_pref', 'language', 'compact_view'),
        }),
        ('Security', {
            'fields': ('active_device', 'failed_login_count', 'lockout_until',
                       'last_login_ip', 'last_login_city', 'last_login_country',
                       'last_seen', 'is_online'),
            'classes': ('collapse',),
        }),
    )
    add_fieldsets = BaseUserAdmin.add_fieldsets + (
        ('Office Info', {'fields': ('email', 'first_name', 'last_name', 'employee_id', 'phone')}),
    )

    @admin.display(description='Locked?', boolean=True)
    def lockout_status(self, obj):
        return obj.is_locked_out()

    actions = ['unlock_accounts', 'revoke_all_devices']

    @admin.action(description='Unlock selected accounts')
    def unlock_accounts(self, request, queryset):
        for user in queryset:
            user.clear_failed_logins()
        self.message_user(request, f'Unlocked {queryset.count()} account(s).')

    @admin.action(description='Revoke all trusted devices for selected users')
    def revoke_all_devices(self, request, queryset):
        count = 0
        for user in queryset:
            n = user.trusted_devices.filter(is_revoked=False).update(is_revoked=True)
            user.active_device = None
            user.save(update_fields=['active_device'])
            count += n
        self.message_user(request, f'Revoked {count} device(s).')


# ─────────────────────────────────────────────────────────────────────────────
# Security Settings (singleton)
# ─────────────────────────────────────────────────────────────────────────────

@admin.register(SecuritySettings)
class SecuritySettingsAdmin(admin.ModelAdmin):
    fieldsets = (
        ('Account Lockout', {
            'fields': ('lockout_enabled', 'max_failed_attempts', 'lockout_duration_minutes'),
            'description': 'Lock accounts after repeated failed login attempts.',
        }),
        ('Geo-velocity Alerts', {
            'fields': ('geo_alert_enabled', 'geo_alert_distance_km', 'geo_alert_minutes'),
            'description': 'Alert users when they log in from a suspiciously distant location in a short time.',
        }),
        ('OTP', {
            'fields': ('otp_expiry_minutes', 'otp_max_attempts'),
        }),
        ('Device Trust', {
            'fields': ('device_trust_days',),
        }),
    )

    def has_add_permission(self, request):
        # Only one row allowed
        return not SecuritySettings.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Trusted Devices
# ─────────────────────────────────────────────────────────────────────────────

@admin.register(TrustedDevice)
class TrustedDeviceAdmin(admin.ModelAdmin):
    list_display  = ['user', 'device_name', 'city', 'country',
                     'ip_address', 'trusted_at', 'expires_at', 'is_revoked', 'is_still_valid']
    list_filter   = ['is_revoked', 'country']
    search_fields = ['user__email', 'device_name', 'ip_address', 'city']
    readonly_fields = ['trusted_at', 'last_used', 'device_id', 'user_agent']
    ordering      = ['-trusted_at']

    @admin.display(description='Valid?', boolean=True)
    def is_still_valid(self, obj):
        return obj.is_valid()

    actions = ['revoke_devices']

    @admin.action(description='Revoke selected devices')
    def revoke_devices(self, request, queryset):
        updated = queryset.update(is_revoked=True)
        self.message_user(request, f'Revoked {updated} device(s).')


# ─────────────────────────────────────────────────────────────────────────────
# Security Events
# ─────────────────────────────────────────────────────────────────────────────

@admin.register(SecurityEvent)
class SecurityEventAdmin(admin.ModelAdmin):
    list_display  = ['created_at', 'user', 'event_type_badge', 'device_name',
                     'ip_address', 'city', 'country']
    list_filter   = ['event_type', 'country', 'created_at']
    search_fields = ['user__email', 'ip_address', 'device_name', 'city', 'detail']
    readonly_fields = list(SecurityEvent._meta.get_fields().__class__.__name__ and
                           [f.name for f in SecurityEvent._meta.get_fields()
                            if hasattr(f, 'column')] or [])
    ordering      = ['-created_at']

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    @admin.display(description='Event')
    def event_type_badge(self, obj):
        colours = {
            'login_success':    '#10b981',
            'login_failed':     '#f59e0b',
            'account_locked':   '#ef4444',
            'otp_sent':         '#3b82f6',
            'otp_success':      '#10b981',
            'otp_failed':       '#f59e0b',
            'otp_expired':      '#94a3b8',
            'device_trusted':   '#10b981',
            'device_revoked':   '#ef4444',
            'switch_requested': '#8b5cf6',
            'switch_approved':  '#10b981',
            'untrusted_attempt':'#ef4444',
            'geo_alert':        '#f97316',
            'logout':           '#64748b',
        }
        color = colours.get(obj.event_type, '#64748b')
        label = obj.get_event_type_display()
        return format_html(
            '<span style="background:{};color:#fff;padding:2px 8px;'
            'border-radius:4px;font-size:.75rem;font-weight:700">{}</span>',
            color, label,
        )


# ─────────────────────────────────────────────────────────────────────────────
# OTP Challenges (read-only)
# ─────────────────────────────────────────────────────────────────────────────

@admin.register(OTPChallenge)
class OTPChallengeAdmin(admin.ModelAdmin):
    list_display  = ['user', 'purpose', 'is_used', 'attempts',
                     'created_at', 'expires_at', 'pending_city']
    list_filter   = ['purpose', 'is_used']
    search_fields = ['user__email', 'pending_city', 'pending_country']
    readonly_fields = [f.name for f in OTPChallenge._meta.get_fields() if hasattr(f, 'column')]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Original models
# ─────────────────────────────────────────────────────────────────────────────

@admin.register(OfficeSettings)
class OfficeSettingsAdmin(admin.ModelAdmin):
    list_display  = ['key', 'label', 'value', 'group', 'value_type']
    list_filter   = ['group', 'value_type']
    search_fields = ['key', 'label']


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display    = ['user', 'action', 'model_name', 'object_repr', 'timestamp']
    list_filter     = ['action', 'model_name']
    readonly_fields = ['user', 'action', 'model_name', 'object_id',
                       'object_repr', 'changes', 'ip_address', 'timestamp']


@admin.register(CoreNotification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ['recipient', 'title', 'notification_type', 'is_read', 'created_at']
    list_filter  = ['notification_type', 'is_read']