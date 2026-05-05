"""
apps/customer_portal/admin.py
==============================
Read-only-ish admin views for ops staff to inspect tokens, contacts,
visits, and disputes. We deliberately don't allow creating tokens from
the admin — they're issued by the contract signal.
"""
from django.contrib import admin

from .models import (
    ContractContact, PortalAccessToken, MaintenanceSchedule,
    MaintenanceVisit, OnSiteEvent, PortalSupportRequest,
)


@admin.register(ContractContact)
class ContractContactAdmin(admin.ModelAdmin):
    list_display = ('full_name', 'email', 'role', 'contract', 'is_active', 'created_at')
    list_filter = ('role', 'is_active', 'receives_pm_notices')
    search_fields = ('full_name', 'email', 'phone')
    raw_id_fields = ('contract', 'customer')


@admin.register(PortalAccessToken)
class PortalAccessTokenAdmin(admin.ModelAdmin):
    list_display = (
        'contact', 'contract', 'valid_from', 'valid_until',
        'use_count', 'last_used_at', 'revoked_at',
    )
    list_filter = ('revoked_at',)
    search_fields = ('contact__email', 'contact__full_name', 'token')
    readonly_fields = ('token', 'issued_at', 'last_used_at', 'use_count')
    raw_id_fields = ('contact', 'contract')

    actions = ['revoke_selected']

    @admin.action(description='Revoke selected tokens')
    def revoke_selected(self, request, queryset):
        n = 0
        for tok in queryset:
            if not tok.revoked_at:
                tok.revoke(reason=f'Manually revoked by {request.user}.')
                n += 1
        self.message_user(request, f'{n} token(s) revoked.')


@admin.register(MaintenanceSchedule)
class MaintenanceScheduleAdmin(admin.ModelAdmin):
    list_display = ('contract', 'cadence', 'has_preventive', 'has_on_call', 'next_due_date')
    list_filter = ('cadence', 'has_preventive', 'has_on_call')
    raw_id_fields = ('contract',)


@admin.register(MaintenanceVisit)
class MaintenanceVisitAdmin(admin.ModelAdmin):
    list_display = ('contract', 'kind', 'status', 'scheduled_for', 'technician', 'completed_at')
    list_filter = ('kind', 'status')
    search_fields = ('summary', 'contract__title')
    raw_id_fields = ('contract', 'schedule', 'technician', 'task', 'ticket')


@admin.register(OnSiteEvent)
class OnSiteEventAdmin(admin.ModelAdmin):
    list_display = ('event_type', 'task', 'contract', 'actor_user', 'actor_contact', 'created_at')
    list_filter = ('event_type',)
    search_fields = ('task__title', 'note')
    raw_id_fields = ('task', 'assignment', 'contract', 'actor_user', 'actor_contact')


@admin.register(PortalSupportRequest)
class PortalSupportRequestAdmin(admin.ModelAdmin):
    list_display = ('subject', 'kind', 'priority', 'contract', 'contact', 'service_ticket', 'created_at')
    list_filter = ('kind', 'priority')
    search_fields = ('subject', 'description', 'contact__email')
    raw_id_fields = ('contract', 'contact', 'token_used', 'service_ticket')
