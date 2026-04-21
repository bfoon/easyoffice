from django.urls import path
from . import views

urlpatterns = [
    path("", views.CustomerServiceDashboardView.as_view(), name="customer_service_dashboard"),
    path("call-desk/", views.CallDeskView.as_view(), name="customer_service_call_desk"),
    path("lookup/", views.PhoneLookupView.as_view(), name="customer_service_phone_lookup"),

    path("customers/", views.CustomerListView.as_view(), name="customer_service_customer_list"),
    path("customers/<int:pk>/", views.CustomerDetailView.as_view(), name="customer_service_customer_detail"),

    path("tickets/", views.TicketListView.as_view(), name="customer_service_ticket_list"),
    path("tickets/<int:pk>/", views.TicketDetailView.as_view(), name="customer_service_ticket_detail"),
    path("tickets/<int:pk>/action/", views.TicketActionView.as_view(), name="customer_service_ticket_action"),

    path("department-queue/", views.DepartmentQueueView.as_view(), name="customer_service_department_queue"),
    path("feedback/<uuid:token>/", views.FeedbackFormView.as_view(), name="customer_service_feedback"),
]