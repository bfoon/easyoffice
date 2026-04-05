from django.urls import path
from apps.reports import views

urlpatterns = [
    path('', views.ReportsDashboardView.as_view(), name='reports_dashboard'),
    path('tasks/', views.TaskReportView.as_view(), name='task_report'),
    path('projects/', views.ProjectReportView.as_view(), name='project_report'),
    path('staff/', views.StaffReportView.as_view(), name='staff_report'),
    path('finance/', views.FinanceReportView.as_view(), name='finance_report'),
]
