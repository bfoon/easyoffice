"""
apps/logistics/urls_driver_portal.py — tokenized driver portal.
Include in project urls.py:  path('drive/', include('apps.logistics.urls_driver_portal')),

The permanent driver link is:  https://your-host/drive/<token>/
"""
from django.urls import path
from apps.logistics import views_driver_portal as v

urlpatterns = [
    path('<str:token>/', v.DriverPortalLandingView.as_view(), name='driver_portal_landing'),
    path('<str:token>/deliveries/', v.DriverDeliveriesView.as_view(), name='driver_deliveries'),
    path('<str:token>/d/<uuid:pk>/', v.DriverShipmentDetailView.as_view(), name='driver_shipment_detail'),
    path('<str:token>/d/<uuid:pk>/status/', v.DriverStatusUpdateView.as_view(), name='driver_status_update'),
    path('<str:token>/d/<uuid:pk>/ping/', v.DriverGpsPingView.as_view(), name='driver_gps_ping'),
    path('<str:token>/d/<uuid:pk>/pod/', v.DriverProofOfDeliveryView.as_view(), name='driver_proof_of_delivery'),
]
