from django.contrib import admin

from apps.hr.models import (
    AppraisalKPI,
    AttendanceRecord,
    DisciplinaryAction,
    EmployeeDocument,
    EmployeeEmergencyContact,
    EmployeeEmployment,
    EmployeeOnboarding,
    EmployeeReference,
    EmployeeWorkHistory,
    HireCategory,
    HRSetting,
    PayrollRecord,
    PerformanceAppraisal,
)


@admin.register(HRSetting)
class HRSettingAdmin(admin.ModelAdmin):
    list_display = ['normal_start_time', 'normal_end_time', 'discipline_enabled', 'late_half_day_after_minutes', 'pay_cycle', 'payroll_day']

    def has_add_permission(self, request):
        return not HRSetting.objects.exists()


@admin.register(HireCategory)
class HireCategoryAdmin(admin.ModelAdmin):
    list_display = ['name', 'category_type', 'requires_end_date', 'requires_number_of_days', 'is_active']
    list_filter = ['category_type', 'is_active']
    search_fields = ['name', 'description']


class EmployeeDocumentInline(admin.TabularInline):
    model = EmployeeDocument
    extra = 0


class EmployeeWorkHistoryInline(admin.TabularInline):
    model = EmployeeWorkHistory
    extra = 0


class EmployeeReferenceInline(admin.TabularInline):
    model = EmployeeReference
    extra = 0


class EmployeeEmergencyContactInline(admin.TabularInline):
    model = EmployeeEmergencyContact
    extra = 0


@admin.register(EmployeeOnboarding)
class EmployeeOnboardingAdmin(admin.ModelAdmin):
    list_display = ['employee_code', 'full_name', 'status', 'hire_category', 'department_ref', 'unit', 'position', 'official_email', 'created_at']
    list_filter = ['status', 'hire_category', 'department_ref', 'unit', 'position', 'created_at']
    search_fields = ['employee_code', 'first_name', 'last_name', 'official_email', 'proposed_username']
    readonly_fields = ['employee_code', 'proposed_username', 'created_at', 'updated_at', 'account_updated_at', 'submitted_at', 'approved_at']
    inlines = [EmployeeDocumentInline, EmployeeWorkHistoryInline, EmployeeReferenceInline, EmployeeEmergencyContactInline]


@admin.register(EmployeeEmployment)
class EmployeeEmploymentAdmin(admin.ModelAdmin):
    list_display = ['employee_code', 'user', 'department_ref', 'unit', 'position', 'hire_category', 'monthly_salary', 'status', 'start_date']
    list_filter = ['status', 'department_ref', 'unit', 'position', 'hire_category']
    search_fields = ['employee_code', 'user__email', 'user__first_name', 'user__last_name', 'job_title', 'department_ref__name', 'position__title']


@admin.register(DisciplinaryAction)
class DisciplinaryActionAdmin(admin.ModelAdmin):
    list_display = ['staff', 'action_type', 'status', 'salary_paused', 'start_date', 'end_date', 'issued_by']
    list_filter = ['action_type', 'status', 'salary_paused']
    search_fields = ['staff__email', 'staff__first_name', 'staff__last_name', 'reason']


@admin.register(PerformanceAppraisal)
class AppraisalAdmin(admin.ModelAdmin):
    list_display = ['staff', 'year', 'status', 'overall_rating', 'supervisor']
    list_filter = ['year', 'status', 'overall_rating']
    search_fields = ['staff__email', 'staff__first_name', 'staff__last_name']


@admin.register(AppraisalKPI)
class KPIAdmin(admin.ModelAdmin):
    list_display = ['appraisal', 'name', 'weight', 'self_score', 'supervisor_score']


@admin.register(PayrollRecord)
class PayrollAdmin(admin.ModelAdmin):
    list_display = ['staff', 'period_month', 'period_year', 'gross_salary', 'net_salary', 'payable_days', 'status', 'payslip_sent_at']
    list_filter = ['status', 'period_year', 'period_month']
    search_fields = ['staff__email', 'staff__first_name', 'staff__last_name']


@admin.register(AttendanceRecord)
class AttendanceAdmin(admin.ModelAdmin):
    list_display = ['staff', 'date', 'status', 'check_in', 'check_out', 'minutes_late', 'worked_hours']
    list_filter = ['status', 'date']
    search_fields = ['staff__email', 'staff__first_name', 'staff__last_name']
