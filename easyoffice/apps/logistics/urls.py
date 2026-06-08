"""
apps/logistics/urls.py — staff console.
Include in project urls.py:  path('logistics/', include('apps.logistics.urls')),
"""
from django.urls import path
from apps.logistics import views as v

urlpatterns = [
    path('', v.LogisticsDashboardView.as_view(), name='logistics_dashboard'),

    path('shipments/', v.ShipmentListView.as_view(), name='logistics_shipment_list'),
    path('shipments/new/', v.ShipmentCreateView.as_view(), name='logistics_shipment_create'),
    path('shipments/<uuid:pk>/', v.ShipmentDetailView.as_view(), name='logistics_shipment_detail'),
    path('shipments/<uuid:pk>/assign/', v.ShipmentAssignView.as_view(), name='logistics_shipment_assign'),
    path('shipments/<uuid:pk>/dispatch/', v.ShipmentDispatchView.as_view(), name='logistics_shipment_dispatch'),
    path('shipments/<uuid:pk>/cancel/', v.ShipmentCancelView.as_view(), name='logistics_shipment_cancel'),
    path('shipments/<uuid:pk>/track/', v.ShipmentTrackView.as_view(), name='logistics_shipment_track'),
    path('shipments/<uuid:pk>/track/data/', v.ShipmentTrackDataView.as_view(), name='logistics_shipment_track_data'),

    path('vehicles/', v.VehicleListView.as_view(), name='logistics_vehicle_list'),
    path('vehicles/save/', v.VehicleSaveView.as_view(), name='logistics_vehicle_save'),

    path('drivers/', v.DriverListView.as_view(), name='logistics_driver_list'),
    path('drivers/save/', v.DriverSaveView.as_view(), name='logistics_driver_save'),
    path('drivers/link-action/', v.DriverLinkActionView.as_view(), name='logistics_driver_link_action'),
]
