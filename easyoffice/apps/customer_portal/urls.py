"""
apps/customer_portal/urls.py
=============================

Mount this whole module at /portal/ in your project urls.py:

    path('portal/', include('apps.customer_portal.urls')),

The token is a long URL-safe string. We use <str:token> rather than
<slug:token> because slug forbids some chars secrets.token_urlsafe() can
emit. <str:> just forbids '/', which is what we want.
"""
from django.urls import path

from . import views, staff_views

urlpatterns = [
    # Token recovery (no token in URL — used when the link is dead)
    path(
        'request-new-link/',
        views.RequestNewLinkView.as_view(),
        name='customer_portal_request_new_link',
    ),

    # ── Token-gated, but BEFORE device verification ─────────────────────
    # The OTP page itself can't require device verification (chicken/egg).
    path(
        '<str:token>/verify-device/',
        views.VerifyDeviceView.as_view(),
        name='customer_portal_verify_device',
    ),

    # ── Token-gated AND device-verified ─────────────────────────────────
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

    # Ticket detail + actions
    path(
        '<str:token>/tickets/<uuid:request_id>/',
        views.TicketDetailView.as_view(),
        name='customer_portal_ticket_detail',
    ),
    path(
        '<str:token>/tickets/<uuid:request_id>/comment/',
        views.TicketCommentView.as_view(),
        name='customer_portal_ticket_comment',
    ),
    path(
        '<str:token>/tickets/<uuid:request_id>/cancel/',
        views.TicketCancelView.as_view(),
        name='customer_portal_ticket_cancel',
    ),
    path(
        '<str:token>/tickets/<uuid:request_id>/reopen/',
        views.TicketReopenView.as_view(),
        name='customer_portal_ticket_reopen',
    ),
    path(
        '<str:token>/tickets/<uuid:request_id>/rate/',
        views.TicketRateView.as_view(),
        name='customer_portal_ticket_rate',
    ),
    path(
        '<str:token>/tickets/<uuid:request_id>/attachments/<uuid:attachment_id>/',
        views.AttachmentDownloadView.as_view(),
        name='customer_portal_attachment_download',
    ),

    # Visits
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
    path('contracts/<uuid:pk>/send-link/',
         staff_views.SendPortalLinkView.as_view(),
         name='customer_portal_send_link'),
]