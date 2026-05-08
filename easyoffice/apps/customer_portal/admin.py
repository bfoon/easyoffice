"""
apps/customer_portal/admin.py
==============================
Read-only-ish admin views for ops staff to inspect tokens, contacts,
visits, comments, ratings, and disputes. We deliberately don't allow
creating tokens from the admin — they're issued by the contract signal.
"""
from django.contrib import admin
from django.utils import timezone

from .models import (
    ContractContact, DeviceBinding, DeviceOTP,
    MaintenanceSchedule, MaintenanceVisit, OnSiteEvent,
    PortalAccessToken, PortalSupportRequest, ReopenRequest,
    TicketAttachment, TicketComment, TicketRating,
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


@admin.register(DeviceBinding)
class DeviceBindingAdmin(admin.ModelAdmin):
    list_display = (
        'token', 'label', 'use_count', 'last_seen_at',
        'ip_last_seen', 'revoked_at',
    )
    list_filter = ('revoked_at',)
    search_fields = ('label', 'user_agent', 'token__token', 'token__contact__email')
    readonly_fields = (
        'fingerprint_hash', 'user_agent', 'verified_at',
        'last_seen_at', 'use_count', 'ip_first_seen', 'ip_last_seen',
    )
    raw_id_fields = ('token',)

    actions = ['revoke_selected_devices']

    @admin.action(description='Revoke selected devices')
    def revoke_selected_devices(self, request, queryset):
        n = 0
        for d in queryset:
            if not d.revoked_at:
                d.revoke(reason=f'Manually revoked by {request.user}.')
                n += 1
        self.message_user(request, f'{n} device(s) revoked.')


@admin.register(DeviceOTP)
class DeviceOTPAdmin(admin.ModelAdmin):
    list_display = (
        'token', 'purpose', 'issued_at', 'expires_at',
        'consumed_at', 'invalidated_at', 'attempts',
    )
    list_filter = ('purpose',)
    search_fields = ('token__token', 'delivered_to_email')
    readonly_fields = (
        'token', 'fingerprint_hash', 'code_hash', 'purpose',
        'issued_at', 'expires_at', 'consumed_at', 'invalidated_at',
        'attempts', 'delivered_to_email', 'requested_ip',
        'requested_user_agent',
    )

    def has_add_permission(self, request):
        return False  # OTPs are issued by the flow, never manually


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


class TicketCommentInline(admin.TabularInline):
    model = TicketComment
    extra = 0
    fields = ('created_at', 'author_kind', 'author_display_name', 'body', 'event_tag')
    readonly_fields = ('created_at',)


class TicketAttachmentInline(admin.TabularInline):
    model = TicketAttachment
    extra = 0
    fields = ('created_at', 'original_name', 'content_type', 'size_bytes', 'uploader_kind')
    readonly_fields = ('created_at', 'size_bytes')


@admin.register(PortalSupportRequest)
class PortalSupportRequestAdmin(admin.ModelAdmin):
    list_display = (
        'subject', 'status', 'kind', 'priority', 'contract',
        'contact', 'service_ticket', 'reopen_count', 'created_at',
    )
    list_filter = ('status', 'kind', 'priority')
    search_fields = ('subject', 'description', 'contact__email')
    raw_id_fields = ('contract', 'contact', 'token_used', 'service_ticket')
    readonly_fields = (
        'created_at', 'updated_at', 'resolved_at', 'closed_at',
        'cancelled_at', 'reopen_count', 'last_reopened_at',
    )
    inlines = (TicketCommentInline, TicketAttachmentInline)

    actions = ['mark_resolved', 'mark_closed', 'reopen']

    @admin.action(description='Mark selected as resolved')
    def mark_resolved(self, request, queryset):
        now = timezone.now()
        n = queryset.exclude(status__in=PortalSupportRequest.TERMINAL_STATUSES).update(
            status='resolved', resolved_at=now, updated_at=now,
        )
        self.message_user(request, f'{n} request(s) marked resolved.')

    @admin.action(description='Close selected')
    def mark_closed(self, request, queryset):
        now = timezone.now()
        n = queryset.update(status='closed', closed_at=now, updated_at=now)
        self.message_user(request, f'{n} request(s) closed.')

    @admin.action(description='Reopen selected')
    def reopen(self, request, queryset):
        now = timezone.now()
        n = 0
        for r in queryset:
            r.status = 'reopened'
            r.last_reopened_at = now
            r.reopen_count = (r.reopen_count or 0) + 1
            r.save(update_fields=['status', 'last_reopened_at', 'reopen_count', 'updated_at'])
            TicketComment.objects.create(
                request=r,
                author_kind='system',
                author_display_name='System',
                event_tag='reopened',
                body=f'Reopened by {request.user}.',
            )
            n += 1
        self.message_user(request, f'{n} request(s) reopened.')


@admin.register(TicketComment)
class TicketCommentAdmin(admin.ModelAdmin):
    list_display = ('request', 'author_kind', 'author_display_name', 'event_tag', 'created_at')
    list_filter = ('author_kind', 'event_tag')
    search_fields = ('body', 'request__subject', 'author_display_name')
    raw_id_fields = ('request', 'author_contact', 'author_user')


@admin.register(TicketAttachment)
class TicketAttachmentAdmin(admin.ModelAdmin):
    list_display = ('original_name', 'request', 'uploader_kind', 'size_bytes', 'created_at')
    list_filter = ('uploader_kind', 'content_type')
    search_fields = ('original_name', 'request__subject')
    raw_id_fields = ('request', 'comment', 'uploader_contact', 'uploader_user')


@admin.register(TicketRating)
class TicketRatingAdmin(admin.ModelAdmin):
    list_display = ('request', 'score', 'would_recommend', 'rated_by_contact', 'created_at')
    list_filter = ('score', 'would_recommend')
    search_fields = ('feedback', 'request__subject')
    raw_id_fields = ('request', 'rated_by_contact')


@admin.register(ReopenRequest)
class ReopenRequestAdmin(admin.ModelAdmin):
    list_display = ('request', 'decision', 'requested_by_contact', 'created_at', 'decided_at')
    list_filter = ('decision',)
    search_fields = ('reason', 'decision_note', 'request__subject')
    raw_id_fields = ('request', 'requested_by_contact', 'decided_by')
    readonly_fields = ('created_at', 'updated_at')

    actions = ['approve_selected', 'decline_selected']

    @admin.action(description='Approve selected (reopens the ticket)')
    def approve_selected(self, request, queryset):
        now = timezone.now()
        n = 0
        for rr in queryset.filter(decision='pending'):
            rr.decision = 'approved'
            rr.decided_at = now
            rr.decided_by = request.user
            rr.save(update_fields=['decision', 'decided_at', 'decided_by', 'updated_at'])

            ticket = rr.request
            ticket.status = 'reopened'
            ticket.last_reopened_at = now
            ticket.reopen_count = (ticket.reopen_count or 0) + 1
            ticket.save(update_fields=['status', 'last_reopened_at', 'reopen_count', 'updated_at'])

            TicketComment.objects.create(
                request=ticket,
                author_kind='system',
                author_display_name='System',
                event_tag='reopened',
                body=f'Ticket reopened by {request.user} after customer request.',
            )
            n += 1
        self.message_user(request, f'{n} reopen request(s) approved.')

    @admin.action(description='Decline selected')
    def decline_selected(self, request, queryset):
        now = timezone.now()
        n = queryset.filter(decision='pending').update(
            decision='declined', decided_at=now, decided_by=request.user,
        )
        self.message_user(request, f'{n} reopen request(s) declined.')