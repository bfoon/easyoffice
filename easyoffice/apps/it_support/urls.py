from django.urls import path
from apps.it_support import views
from apps.it_support import views_technician_links as tl

urlpatterns = [
    path('', views.ITDashboardView.as_view(), name='it_dashboard'),
    path('tickets/', views.TicketListView.as_view(), name='ticket_list'),
    path('tickets/create/', views.TicketCreateView.as_view(), name='ticket_create'),
    path('tickets/<uuid:pk>/', views.TicketDetailView.as_view(), name='ticket_detail'),
    path('tickets/<uuid:pk>/update/', views.TicketUpdateView.as_view(), name='ticket_update'),
    path('equipment/', views.EquipmentListView.as_view(), name='equipment_list'),
    path('equipment/<uuid:pk>/action/', views.EquipmentActionView.as_view(), name='equipment_action'),

    # Technician portal links (IT-managed)
    path('technician-links/', tl.TechnicianLinkListView.as_view(), name='technician_link_list'),
    path('technician-links/action/', tl.TechnicianLinkActionView.as_view(), name='technician_link_action'),
]