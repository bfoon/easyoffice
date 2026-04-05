from django.urls import path
from apps.hr import views

urlpatterns = [
    path('', views.HRDashboardView.as_view(), name='hr_dashboard'),
    path('appraisals/', views.AppraisalListView.as_view(), name='appraisal_list'),
    path('appraisals/<uuid:pk>/', views.AppraisalDetailView.as_view(), name='appraisal_detail'),
    path('payroll/', views.PayrollView.as_view(), name='payroll'),
    path('attendance/', views.AttendanceView.as_view(), name='attendance'),
    path('attendance/sheet/', views.AttendanceSheetView.as_view(), name='attendance_sheet'),
]