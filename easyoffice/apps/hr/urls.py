from django.urls import path
from apps.hr import views

urlpatterns = [
    path('', views.HRDashboardView.as_view(), name='hr_dashboard'),

    # Employee onboarding / hiring lifecycle
    path('onboarding/', views.EmployeeOnboardingListView.as_view(), name='employee_onboarding_list'),
    path('onboarding/new/', views.EmployeeOnboardingCreateView.as_view(), name='employee_onboarding_create'),
    path('onboarding/<uuid:pk>/', views.EmployeeOnboardingDetailView.as_view(), name='employee_onboarding_detail'),

    # Staff employment lifecycle
    path('staff/', views.EmploymentListView.as_view(), name='employment_list'),
    path('staff/<uuid:pk>/action/', views.EmploymentActionView.as_view(), name='employment_action'),
    path('staff/<uuid:staff_id>/discipline/', views.DisciplinaryActionCreateView.as_view(), name='disciplinary_action_create'),

    # Appraisals
    path('appraisals/', views.AppraisalListView.as_view(), name='appraisal_list'),
    path('appraisals/<uuid:pk>/', views.AppraisalDetailView.as_view(), name='appraisal_detail'),

    # Payroll
    path('payroll/', views.PayrollView.as_view(), name='payroll'),
    path('payroll/generate/', views.PayrollGenerateView.as_view(), name='payroll_generate'),
    path('payroll/<uuid:pk>/action/', views.PayrollActionView.as_view(), name='payroll_action'),
    path('payroll/<uuid:pk>/payslip/', views.PayrollPayslipPrintView.as_view(), name='payroll_payslip_print'),
    path('payroll/payslips/email/', views.PayrollBulkPayslipEmailView.as_view(), name='payroll_payslips_email'),

    # Attendance
    path('attendance/', views.AttendanceView.as_view(), name='attendance'),
    path('attendance/sheet/', views.AttendanceSheetView.as_view(), name='attendance_sheet'),
]
