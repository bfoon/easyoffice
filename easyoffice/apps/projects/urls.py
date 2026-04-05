from django.urls import path
from apps.projects import views

urlpatterns = [
    path('', views.ProjectListView.as_view(), name='project_list'),
    path('create/', views.ProjectCreateView.as_view(), name='project_create'),
    path('<uuid:pk>/', views.ProjectDetailView.as_view(), name='project_detail'),
    path('<uuid:pk>/edit/', views.ProjectEditView.as_view(), name='project_edit'),
    path('<uuid:pk>/update/', views.ProjectUpdateStatusView.as_view(), name='project_update'),
    path('<uuid:pk>/milestone/', views.ProjectMilestoneView.as_view(), name='project_milestone'),
    path('<uuid:pk>/milestone/<uuid:mid>/status/', views.MilestoneStatusView.as_view(), name='milestone_status'),
    path('<uuid:pk>/risk/', views.ProjectRiskView.as_view(), name='project_risk'),
]