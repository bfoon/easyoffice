from django.contrib import admin
from apps.hr.models import PerformanceAppraisal, AppraisalKPI, PayrollRecord


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
    list_display = ['staff', 'period_month', 'period_year', 'gross_salary', 'net_salary', 'status']
    list_filter = ['status', 'period_year']
