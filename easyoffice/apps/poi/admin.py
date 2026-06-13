from django.contrib import admin, messages

from apps.poi.models import POIProfile, POIInvite


class POIInviteInline(admin.TabularInline):
    model = POIInvite
    extra = 0
    fields = ["token", "expires_at", "used_at", "created_at"]
    readonly_fields = ["token", "created_at"]


@admin.register(POIProfile)
class POIProfileAdmin(admin.ModelAdmin):
    list_display = ["user", "status", "trusted_until", "created_by", "created_at"]
    list_filter = ["status"]
    search_fields = ["user__email", "user__first_name", "user__last_name", "notes"]
    readonly_fields = ["device_fingerprint", "disabled_by", "disabled_at",
                       "created_at", "updated_at"]
    inlines = [POIInviteInline]
    actions = ["action_disable"]

    @admin.action(description="Disable selected POIs (untrust device + deactivate)")
    def action_disable(self, request, queryset):
        n = 0
        for prof in queryset:
            prof.disable(by=request.user, reason="Disabled via admin")
            n += 1
        self.message_user(request, f"Disabled {n} POI(s).", messages.SUCCESS)


@admin.register(POIInvite)
class POIInviteAdmin(admin.ModelAdmin):
    list_display = ["token", "profile", "expires_at", "used_at", "created_at"]
    search_fields = ["profile__user__email"]
    readonly_fields = ["token", "created_at"]
