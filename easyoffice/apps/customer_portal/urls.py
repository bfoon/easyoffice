"""
apps/customer_portal/urls.py
=============================

Mount this whole module at /portal/ in your project urls.py:

    path('portal/', include('apps.customer_portal.urls')),

The token is a long URL-safe string, so we accept it as a path slug.
We don't use <slug:token> because slug only allows [-a-zA-Z0-9_], and
secrets.token_urlsafe() can include underscores and hyphens — fine —
but to be safe we use <str:token> which forbids only '/'.
"""
from django.urls import path

from . import views

urlpatterns = [
    # Token-recovery (no token in URL — used when their link is dead)
    path(
        'request-new-link/',
        views.RequestNewLinkView.as_view(),
        name='customer_portal_request_new_link',
    ),

    # Everything below this is token-gated.
    path(
        '<str:token>/',
        views.PortalHomeView.as_view(),
        name='customer_portal_home',
    ),
    path(
        '<str:token>/new-request/',
        views.NewSupportRequestView.as_view(),
        name='customer_portal_new_request',
    ),
    path(
        '<str:token>/visits/<uuid:visit_id>/',
        views.VisitDetailView.as_view(),
        name='customer_portal_visit_detail',
    ),
    path(
        '<str:token>/dispute/<uuid:event_id>/',
        views.DisputeOnSiteView.as_view(),
        name='customer_portal_dispute',
    ),
]
