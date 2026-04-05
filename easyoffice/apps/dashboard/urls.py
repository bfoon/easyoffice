from django.urls import path
from apps.dashboard import views

urlpatterns = [
    path('', views.DashboardView.as_view(), name='dashboard'),
    path('admin/', views.AdminDashboardView.as_view(), name='admin_dashboard'),
]