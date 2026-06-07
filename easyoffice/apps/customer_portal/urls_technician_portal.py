"""
apps/customer_portal/urls_technician_portal.py
===============================================

Include from project urls.py:

    path('tech/', include('apps.customer_portal.urls_technician_portal')),

The permanent technician link is:  https://your-host/tech/<token>/
"""
from django.urls import path

from apps.customer_portal import views_technician_portal as v

urlpatterns = [
    path('<str:token>/',
         v.TechnicianPortalLandingView.as_view(), name='technician_portal_landing'),
    path('<str:token>/entries/',
         v.TechnicianEntryListView.as_view(), name='technician_portal_entries'),
    path('<str:token>/entries/new/',
         v.TechnicianEntryCreateView.as_view(), name='technician_entry_create'),
    path('<str:token>/entries/<uuid:pk>/edit/',
         v.TechnicianEntryEditView.as_view(), name='technician_entry_edit'),
    path('<str:token>/entries/<uuid:pk>/delete/',
         v.TechnicianEntryDeleteView.as_view(), name='technician_entry_delete'),
]
