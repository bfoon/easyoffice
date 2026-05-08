"""
apps/customer_service/views_extra_portal_addon.py
==================================================

DROP-IN SNIPPET — paste the body of this file at the bottom of your
existing apps/customer_service/views_extra.py.

This gives you `views_extra.PortalReplyView` (which your urls_extra.py
already references with name='customer_service_portal_reply').

If you'd rather keep the reply view in a separate module, you can use
the standalone apps/customer_service/portal_views.py + portal_urls.py
combo instead — but if you've already wired urls_extra.py to point at
`views_extra.PortalReplyView`, this is the path of least resistance.
"""
from __future__ import annotations

import logging

from django.contrib import messages
from django.db import transaction
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.views.generic import View

# Assumes ServiceTicket is already imported at the top of views_extra.py.
# If not, add: from .models import ServiceTicket

logger = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════════════════
# 5. PORTAL REPLY (staff replies into the customer portal thread)
# ════════════════════════════════════════════════════════════════════════

# Reply-body field names accepted from POST. Two templates submit to
# this endpoint:
#   * _portal_thread.html        → posts 'portal_body' (bottom composer)
#   * _portal_reply_button.html  → posts 'body'        (per-message inline)
# Either should just work.
_BODY_FIELD_CANDIDATES = ('body', 'portal_body', 'reply_body')


def _read_reply_body(request) -> str:
    for name in _BODY_FIELD_CANDIDATES:
        val = (request.POST.get(name) or '').strip()
        if val:
            return val
    return ''


class PortalReplyView(View):
    """
    POST /customer-service/tickets/<pk>/portal-reply/

    Staff agent posts a reply that the customer will see on their portal
    ticket detail page. We:
      1. Look up the linked PortalSupportRequest
      2. Call sync.mirror_staff_reply_to_portal — creates a
         TicketComment(author_kind='staff') visible on the portal,
         suppressed by the re-entrancy guard so it doesn't bounce back
      3. Also post a ServiceTicketUpdate of type 'customer_update' so
         the reply is visible in the staff activity log

    Any logged-in user with access to the customer_service app can
    reply — we don't gate by ticket ownership.

    Honors an optional `return_to` POST field for an in-page anchor so
    the agent lands back at the comment they were replying to.
    """
    http_method_names = ['post']

    def post(self, request, pk):
        if not request.user.is_authenticated:
            return redirect('login')

        ticket = get_object_or_404(ServiceTicket, pk=pk)
        body = _read_reply_body(request)

        return_to = (request.POST.get('return_to') or '').strip()
        if not return_to.startswith('#'):
            return_to = ''

        if len(body) < 2:
            messages.error(
                request,
                'Reply text is required. Please type your message and click Send.',
            )
            return self._redirect_back(ticket, return_to)

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
                "This ticket isn't linked to a portal request — "
                "the customer won't see this reply on the portal. "
                "Add a regular update note instead.",
            )
            return self._redirect_back(ticket, return_to)

        if portal_req.status in ('cancelled',):
            messages.warning(
                request,
                "The customer cancelled this ticket — they won't see new replies.",
            )
            return self._redirect_back(ticket, return_to)

        try:
            with transaction.atomic():
                portal_sync.mirror_staff_reply_to_portal(
                    portal_request=portal_req,
                    staff_user=request.user,
                    body=body,
                )
                from .models import ServiceTicketUpdate
                ServiceTicketUpdate.objects.create(
                    ticket=ticket,
                    user=request.user,
                    update_type='customer_update',
                    body=f'📤 Replied to customer via portal:\n\n{body}',
                )

                # First staff reply on a still-pending request: open it.
                if portal_req.status == 'pending_review':
                    portal_req.status = 'open'
                    portal_req.save(update_fields=['status', 'updated_at'])
                    portal_sync.push_portal_status_to_staff(
                        portal_req,
                        reason='First staff reply received',
                        actor=request.user,
                    )
        except Exception:
            logger.exception('customer_service: portal reply failed')
            messages.error(
                request,
                'Sorry — your reply could not be sent. Please try again, '
                'or check the server logs if this keeps happening.',
            )
            return self._redirect_back(ticket, return_to)

        try:
            _notify_customer_of_staff_reply(portal_req, request.user, body)
        except Exception:
            logger.warning('customer_service: customer notification failed', exc_info=True)

        messages.success(
            request,
            f'Reply sent — {portal_req.contact.full_name or "the customer"} '
            f'will see it on their portal.',
        )
        return self._redirect_back(ticket, return_to)

    @staticmethod
    def _redirect_back(ticket, fragment: str = ''):
        url = reverse('customer_service_ticket_detail', kwargs={'pk': ticket.pk})
        if fragment:
            url = f'{url}{fragment}'
        return HttpResponseRedirect(url)


def _notify_customer_of_staff_reply(portal_req, agent, body: str) -> None:
    """Best-effort email to the contact letting them know there's a reply."""
    contact = portal_req.contact
    if not contact or not contact.email:
        return

    try:
        from apps.customer_portal import notifications as cp_notif
    except Exception:
        return

    fn = getattr(cp_notif, 'send_staff_reply_alert', None)
    if callable(fn):
        fn(portal_req=portal_req, agent=agent, body=body)


# ────────────────────────────────────────────────────────────────────────
# Helper for ticket detail context — used by the _portal_thread.html
# partial. Add to your existing TicketDetailView.get_context_data:
#
#     from .views_extra import get_portal_context
#     ...
#     def get_context_data(self, **kwargs):
#         ctx = super().get_context_data(**kwargs)
#         ctx.update(get_portal_context(self.object))
#         return ctx
# ────────────────────────────────────────────────────────────────────────

def get_portal_context(ticket) -> dict:
    """
    Returns context keys needed by _portal_thread.html:
        portal_request          — the PortalSupportRequest or None
        portal_comments         — queryset of TicketComment, oldest-first
        portal_comment_count    — total comments

    Safe to call on any ticket, including those without portal origins.
    """
    if not ticket:
        return {
            'portal_request': None,
            'portal_comments': [],
            'portal_comment_count': 0,
        }

    try:
        from apps.customer_portal.models import PortalSupportRequest
    except Exception:
        return {
            'portal_request': None,
            'portal_comments': [],
            'portal_comment_count': 0,
        }

    portal_req = (
        PortalSupportRequest.objects
        .filter(service_ticket=ticket)
        .prefetch_related('comments__attachments', 'reopen_requests')
        .first()
    )
    if not portal_req:
        return {
            'portal_request': None,
            'portal_comments': [],
            'portal_comment_count': 0,
        }

    comments = list(portal_req.comments.all().order_by('created_at'))
    return {
        'portal_request': portal_req,
        'portal_comments': comments,
        'portal_comment_count': len(comments),
    }