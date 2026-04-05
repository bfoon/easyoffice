from django.contrib import admin
from apps.staff.models import StaffProfile, LeaveType, LeaveRequest, LeaveBalance


@admin.register(StaffProfile)
class StaffProfileAdmin(admin.ModelAdmin):
    list_display = ['user', 'department', 'unit', 'position', 'contract_type', 'supervisor', 'is_supervisor_role']
    list_filter = ['department', 'unit', 'contract_type', 'is_supervisor_role']
    search_fields = ['user__email', 'user__first_name', 'user__last_name']
    autocomplete_fields = ['user', 'supervisor']
    fieldsets = (
        ('Identity', {'fields': ('user', 'position', 'department', 'unit', 'location', 'supervisor', 'is_supervisor_role')}),
        ('Contract', {'fields': ('contract_type', 'contract_start', 'contract_end', 'probation_end')}),
        ('Personal', {'fields': ('date_of_birth', 'gender', 'nationality', 'national_id', 'tax_id')}),
        ('Contact', {'fields': ('office_email', 'office_extension')}),
        ('Banking', {'fields': ('bank_name', 'bank_account')}),
        ('Emergency Contact', {'fields': ('emergency_contact_name', 'emergency_contact_phone', 'emergency_contact_relation')}),
        ('Departure', {'fields': ('resignation_date', 'resignation_reason')}),
    )


@admin.register(LeaveType)
class LeaveTypeAdmin(admin.ModelAdmin):
    list_display = ['name', 'code', 'days_per_year', 'is_paid', 'carry_forward', 'is_active']
    list_filter = ['is_paid', 'carry_forward', 'is_active']


@admin.register(LeaveRequest)
class LeaveRequestAdmin(admin.ModelAdmin):
    list_display = ['staff', 'leave_type', 'start_date', 'end_date', 'days_requested', 'status', 'approved_by']
    list_filter = ['status', 'leave_type']
    search_fields = ['staff__first_name', 'staff__last_name', 'staff__email']
    readonly_fields = ['created_at', 'approval_date']
    date_hierarchy = 'start_date'


@admin.register(LeaveBalance)
class LeaveBalanceAdmin(admin.ModelAdmin):
    list_display = ['staff', 'leave_type', 'year', 'days_entitled', 'days_used', 'days_carried_forward']
    list_filter = ['year', 'leave_type']
    search_fields = ['staff__first_name', 'staff__last_name', 'staff__email']
    actions = ['recalculate_used']

    def recalculate_used(self, request, queryset):
        for balance in queryset:
            balance.recalculate_used()
        self.message_user(request, f'Recalculated {queryset.count()} balance(s).')
    recalculate_used.short_description = 'Recalculate days used from approved leaves'