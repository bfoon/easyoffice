"""
apps/customer_service/portal_views.py
======================================

Staff-side endpoints for interacting with the customer portal:

    POST /customer-service/tickets/<pk>/portal/reply/

Wires up to apps/customer_portal/sync.py which does the actual
mirroring. This module exists in customer_service (not customer_portal)
because:
  * the URL lives under /customer-service/
  * staff permissions live here
  * keeping the dependency one-directional (portal can be removed
    without breaking customer_service.urls)

Wire into urls.py with:

    from . import portal_views
    path('tickets/<int:pk>/portal/reply/',
         portal_views.PortalReplyView.as_view(),
         name='customer_service_portal_reply'),
"""
from __future__ import annotations

import logging

from django.contrib import messages
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect
from django.views.generic import View

from .models import ServiceTicket

logger = logging.getLogger(__name__)


class PortalReplyView(View):
    """
    Staff agent posts a reply that the customer will see on the portal
    ticket detail page. We:
      1. Look up the linked PortalSupportRequest
      2. Call sync.mirror_staff_reply_to_portal — which creates a
         TicketComment(author_kind='staff') and is suppressed by the
         re-entrancy guard so it doesn't bounce back to staff.
      3. Also post a ServiceTicketUpdate of type 'customer_update'
         so the reply is visible in the staff activity log too.

    Permissions: any logged-in user with access to the customer_service
    app. We don't gate by ticket ownership here — any agent on the
    team should be able to reply to a customer if the assigned owner
    is unavailable.
    """
    http_method_names = ['post']

    def post(self, request, pk):
        if not request.user.is_authenticated:
            return redirect('login')

        ticket = get_object_or_404(ServiceTicket, pk=pk)
        body = (request.POST.get('body') or '').strip()

        # Optional anchor — set by the inline reply button on each
        # activity row, so the agent lands back at the comment they
        # were replying to.
        return_to = (request.POST.get('return_to') or '').strip()
        if not return_to.startswith('#'):
            return_to = ''

        if len(body) < 2:
            messages.error(request, 'Reply text is required.')
            return self._redirect_back(ticket, return_to)

        # Find the portal-side request (lazy import — keeps this module
        # decoupled from customer_portal at import time).
        try:
            from apps.customer_portal.models import PortalSupportRequest
            from apps.customer_portal import sync as portal_sync
        except Exception:
            messages.error(request, 'The customer portal app is not available right now.')
            logger.exception('customer_service: portal app import failed')
            return self._redirect_back(ticket, return_to)

        portal_req = (
            PortalSupportRequest.objects
            .filter(service_ticket=ticket)
            .first()
        )
        if not portal_req:
            messages.warning(
                request,
                'This ticket isn\'t linked to a portal request — '
                'the customer won\'t see this reply on the portal. '
                'Add a regular update note instead.',
            )
            return self._redirect_back(ticket, return_to)

        if portal_req.status in ('cancelled',):
            messages.warning(
                request,
                'The customer cancelled this ticket — they won\'t see new replies.',
            )
            return self._redirect_back(ticket, return_to)

        try:
            with transaction.atomic():
                portal_sync.mirror_staff_reply_to_portal(
                    portal_request=portal_req,
                    staff_user=request.user,
                    body=body,
                )
                # Also drop a regular activity-log entry on the staff side
                # so the agent's reply is visible in the same timeline as
                # their internal notes.
                from .models import ServiceTicketUpdate
                ServiceTicketUpdate.objects.create(
                    ticket=ticket,
                    user=request.user,
                    update_type='customer_update',
                    body=f'📤 Replied to customer via portal:\n\n{body}',
                )
        except Exception:
            logger.exception('customer_service: portal reply failed')
            messages.error(request, 'Sorry — your reply could not be sent. Please try again.')
            return self._redirect_back(ticket, return_to)

        # Best-effort email notification to the customer
        try:
            _notify_customer_of_staff_reply(portal_req, request.user, body)
        except Exception:
            logger.warning('customer_service: customer notification failed', exc_info=True)

        messages.success(request, 'Reply sent — the customer will see it on the portal.')
        return self._redirect_back(ticket, return_to)

    @staticmethod
    def _redirect_back(ticket, fragment: str = ''):
        """Build a redirect to ticket detail, optionally with a #anchor."""
        from django.urls import reverse
        from django.http import HttpResponseRedirect

        url = reverse('customer_service_ticket_detail', kwargs={'pk': ticket.pk})
        if fragment:
            url = f'{url}{fragment}'
        return HttpResponseRedirect(url)


def _notify_customer_of_staff_reply(portal_req, agent, body: str) -> None:
    """Best-effort email to the contact letting them know there's a reply."""
    contact = portal_req.contact
    if not contact or not contact.email:
        return

    # Lazy import — same pattern as views.py
    try:
        from apps.customer_portal import notifications as cp_notif
    except Exception:
        return

    # We don't need a bespoke template; reuse the customer-comment notifier
    # if it exists. The portal already has _notify_staff_of_customer_comment
    # going the other way; this is the inverse.
    fn = getattr(cp_notif, 'send_staff_reply_alert', None)
    if callable(fn):
        fn(portal_req=portal_req, agent=agent, body=body)