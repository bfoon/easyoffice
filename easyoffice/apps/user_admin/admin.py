from django.contrib import admin

from apps.user_admin.models import (
    AccountInvitation, RegistrationLink, RegistrationLinkUsage, TempPassword,
)


@admin.register(AccountInvitation)
class AccountInvitationAdmin(admin.ModelAdmin):
    list_display  = ('email', 'first_name', 'last_name', 'status',
                     'invited_by', 'expires_at', 'created_at')
    list_filter   = ('status',)
    search_fields = ('email', 'first_name', 'last_name')
    readonly_fields = ('token', 'accepted_at', 'accepted_by', 'created_at')


@admin.register(RegistrationLink)
class RegistrationLinkAdmin(admin.ModelAdmin):
    list_display  = ('label', 'use_count', 'max_uses', 'status',
                     'created_by', 'expires_at', 'created_at')
    list_filter   = ('status',)
    search_fields = ('label', 'group_names')
    readonly_fields = ('token', 'use_count', 'created_at')


@admin.register(RegistrationLinkUsage)
class RegistrationLinkUsageAdmin(admin.ModelAdmin):
    list_display  = ('link', 'user', 'created_at')
    search_fields = ('user__email',)


@admin.register(TempPassword)
class TempPasswordAdmin(admin.ModelAdmin):
    list_display  = ('user', 'issued_by', 'is_consumed', 'password_hint',
                     'created_at', 'consumed_at')
    list_filter   = ('is_consumed',)
    search_fields = ('user__email',)
    readonly_fields = ('password_hint', 'created_at', 'consumed_at')
