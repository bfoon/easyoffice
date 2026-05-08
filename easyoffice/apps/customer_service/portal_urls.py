"""
apps/customer_service/portal_urls.py
====================================

Drop-in URLconf for the staff-side portal-bridge endpoints. Two ways to
wire up:

OPTION A — paste these lines into your existing apps/customer_service/urls.py:

    from . import portal_views

    path('tickets/<int:pk>/portal/reply/',
         portal_views.PortalReplyView.as_view(),
         name='customer_service_portal_reply'),


OPTION B — include this whole module from your existing urls.py:

    from django.urls import include, path
    urlpatterns += [path('', include('apps.customer_service.portal_urls'))]
"""
from django.urls import path

from . import portal_views

urlpatterns = [
    path(
        'tickets/<int:pk>/portal/reply/',
        portal_views.PortalReplyView.as_view(),
        name='customer_service_portal_reply',
    ),
]
