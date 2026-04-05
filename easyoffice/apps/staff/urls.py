from django.urls import path
from apps.staff import views

urlpatterns = [
    path('', views.StaffDirectoryView.as_view(), name='staff_directory'),
    path('<uuid:pk>/', views.StaffDetailView.as_view(), name='staff_detail'),
    path('leave/request/', views.LeaveRequestView.as_view(), name='leave_requests'),
    path('leave/approve/', views.LeaveApprovalView.as_view(), name='leave_approval'),
]