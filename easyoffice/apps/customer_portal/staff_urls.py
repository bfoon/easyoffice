"""
apps/customer_portal/staff_urls.py
==================================

Drop-in URL routes for the staff-side portal actions.

OPTION A — paste these lines into your existing apps/customer_portal/urls.py:

    from . import staff_views

    path(
        'contracts/<uuid:pk>/send-link/',
        staff_views.SendPortalLinkView.as_view(),
        name='customer_portal_send_link',
    ),

OPTION B — include this whole module from your project urls.py
(or from apps/customer_portal/urls.py):

    from django.urls import include, path
    urlpatterns += [path('', include('apps.customer_portal.staff_urls'))]
"""
from django.urls import path

from . import staff_views

urlpatterns = [
    path(
        'contracts/<uuid:pk>/send-link/',
        staff_views.SendPortalLinkView.as_view(),
        name='customer_portal_send_link',
    ),
]
