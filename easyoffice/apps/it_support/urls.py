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

    # Equipment maintenance management
    path('maintenance/', views.MaintenanceListView.as_view(), name='maintenance_list'),
    path('maintenance/new/', views.MaintenanceCreateView.as_view(), name='maintenance_create'),
    # Literal path — must precede 'maintenance/<uuid:pk>/' so it isn't
    # swallowed by the UUID converter.
    path('maintenance/customers/search/', views.MaintenanceCustomerSearchView.as_view(),
         name='maintenance_customer_search'),
    path('maintenance/files/search/', views.MaintenanceFileSearchView.as_view(),
         name='maintenance_file_search'),
    path('maintenance/<uuid:pk>/', views.MaintenanceDetailView.as_view(), name='maintenance_detail'),
    path('maintenance/<uuid:pk>/action/', views.MaintenanceActionView.as_view(), name='maintenance_action'),
    path('maintenance/<uuid:pk>/print/', views.MaintenancePrintView.as_view(), name='maintenance_print'),
    path('track/maintenance/<uuid:token>/', views.MaintenancePublicView.as_view(), name='maintenance_public'),
    # No-login attachment download, authorised by the tracking token itself.
    # Owner-visible attachments only; see MaintenancePublicAttachmentView.
    path('track/maintenance/<uuid:token>/file/<uuid:attachment_id>/',
         views.MaintenancePublicAttachmentView.as_view(),
         name='maintenance_public_attachment'),

    # Technician portal links (IT-managed)
    path('technician-links/', tl.TechnicianLinkListView.as_view(), name='technician_link_list'),
    path('technician-links/action/', tl.TechnicianLinkActionView.as_view(), name='technician_link_action'),
]