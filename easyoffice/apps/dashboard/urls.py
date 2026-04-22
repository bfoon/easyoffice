from django.urls import path
from apps.dashboard import views

urlpatterns = [
    path('', views.DashboardView.as_view(), name='dashboard'),
    path('office-admin/', views.AdminDashboardView.as_view(), name='admin_dashboard'),
    path('task-followup/', views.TaskFollowUpView.as_view(), name='task_followup'),
]