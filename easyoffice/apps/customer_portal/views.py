"""
apps/customer_portal/views.py
==============================

All portal views are TOKEN-GATED, not login-gated. The token in the URL
is the only thing that authenticates the customer. Therefore EVERY view
that touches portal data must:

  1. Resolve the token via _resolve_token()
  2. Check token.is_active
  3. Use the token's contract — never trust contract_id from the URL
     or POST body (an attacker with one valid token shouldn't be able to
     pivot to a different contract by editing form fields)

The dispatch helper TokenRequiredMixin enforces (1) and (2). Views still
need to be careful with (3) — there are notes on the relevant lines.
"""
from __future__ import annotations

import logging

from django.contrib import messages
from django.db import transaction
from django.http import HttpResponse, HttpResponseForbidden, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.generic import View, TemplateView

from .models import (
    PortalAccessToken, ContractContact, MaintenanceSchedule,
    MaintenanceVisit, OnSiteEvent, PortalSupportRequest,
)

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Token gate
# ─────────────────────────────────────────────────────────────────────────────

class TokenRequiredMixin:
    """
    Resolve self.token / self.contact / self.contract or render a 'token
    invalid / expired' page. Must come BEFORE the View base class in MRO.

    Why a mixin and not a decorator? So we can also expose the token on
    self for use in get_context_data without re-resolving it.
    """
    token_url_kwarg = 'token'

    def dispatch(self, request, *args, **kwargs):
        raw_token = kwargs.get(self.token_url_kwarg, '')
        try:
            token = (
                PortalAccessToken.objects
                .select_related('contact', 'contract')
                .get(token=raw_token)
            )
        except PortalAccessToken.DoesNotExist:
            return self._render_invalid(request, reason='not_found')

        if not token.is_active:
            if token.revoked_at:
                return self._render_invalid(request, reason='revoked', token=token)
            return self._render_invalid(request, reason='expired', token=token)

        # Stamp usage AFTER we know the request is valid.
        try:
            token.touch()
        except Exception:
            logger.exception('customer_portal: token.touch failed')

        self.token = token
        self.contact = token.contact
        self.contract = token.contract

        return super().dispatch(request, *args, **kwargs)

    def _render_invalid(self, request, *, reason: str, token=None):
        return render(
            request,
            'customer_portal/invalid_token.html',
            {
                'reason': reason,
                'token': token,
                'support_email': self._support_email(),
            },
            status=410 if reason in ('expired', 'revoked') else 404,
        )

    @staticmethod
    def _support_email():
        from django.conf import settings
        return getattr(settings, 'SUPPORT_EMAIL', None) or 'support@example.com'


# ─────────────────────────────────────────────────────────────────────────────
# Portal home — overview + tabs
# ─────────────────────────────────────────────────────────────────────────────

class PortalHomeView(TokenRequiredMixin, TemplateView):
    template_name = 'customer_portal/home.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        contract = self.contract

        # All open requests this contact has submitted
        ctx['open_requests'] = (
            PortalSupportRequest.objects
            .filter(contract=contract)
            .order_by('-created_at')[:20]
        )

        # PM schedule, if any
        ctx['pm_schedule'] = getattr(contract, 'pm_schedule', None)

        # Recent + upcoming visits
        visits = (
            MaintenanceVisit.objects
            .filter(contract=contract)
            .select_related('technician', 'task')
            .order_by('-scheduled_for', '-created_at')
        )
        ctx['upcoming_visits'] = [v for v in visits if v.status in ('scheduled', 'dispatched')][:10]
        ctx['recent_visits'] = [v for v in visits if v.status in ('on_site', 'completed', 'verified')][:10]

        # Tickets (raw view of staff-side tickets linked to this contract)
        try:
            from apps.customer_service.models import ServiceTicket
            ctx['tickets'] = (
                ServiceTicket.objects
                .filter(contract=contract)  # see migration adding this FK
                .order_by('-created_at')[:20]
            )
        except Exception:
            ctx['tickets'] = []

        ctx['contact'] = self.contact
        ctx['contract'] = contract
        ctx['token'] = self.token

        # Recent on-site events for this contract — gives the customer a
        # transparent log of "tech said they were here at X".
        ctx['on_site_events'] = (
            OnSiteEvent.objects
            .filter(contract=contract)
            .select_related('task', 'actor_user')
            .order_by('-created_at')[:30]
        )

        return ctx


# ─────────────────────────────────────────────────────────────────────────────
# New support request
# ─────────────────────────────────────────────────────────────────────────────

class NewSupportRequestView(TokenRequiredMixin, View):
    """
    Customer fills in subject + description + priority. We:

      1. Save a PortalSupportRequest.
      2. Create a real customer_service.ServiceTicket so the staff
         workflow (assignments, SLA, etc.) kicks in.
      3. Notify the CS team via the existing notifications module.
    """

    def get(self, request, **kwargs):
        return render(
            request,
            'customer_portal/new_request.html',
            {
                'contract': self.contract,
                'contact': self.contact,
                'token': self.token,
                'kinds': PortalSupportRequest.REQUEST_KINDS,
                'priorities': PortalSupportRequest.PRIORITIES,
            },
        )

    def post(self, request, **kwargs):
        kind = request.POST.get('kind', 'support')
        priority = request.POST.get('priority', 'medium')
        subject = (request.POST.get('subject') or '').strip()
        description = (request.POST.get('description') or '').strip()

        if not subject or not description:
            messages.error(request, 'Please give your request a subject and a description.')
            return redirect('customer_portal_new_request', token=self.token.token)

        # Persist the portal record first.
        portal_req = PortalSupportRequest.objects.create(
            contract=self.contract,
            contact=self.contact,
            token_used=self.token,
            kind=kind if kind in dict(PortalSupportRequest.REQUEST_KINDS) else 'support',
            priority=priority if priority in dict(PortalSupportRequest.PRIORITIES) else 'medium',
            subject=subject[:220],
            description=description,
            submitted_from_ip=_client_ip(request),
        )

        # Spin up a real ticket on the staff side.
        try:
            ticket = self._create_service_ticket(portal_req)
            portal_req.service_ticket = ticket
            portal_req.save(update_fields=['service_ticket', 'updated_at'])
        except Exception:
            logger.exception('customer_portal: failed to spin up service ticket from portal request')
            ticket = None

        # Notify CS team (best-effort).
        if ticket is not None:
            transaction.on_commit(lambda: _notify_cs_of_new_request(ticket, portal_req))

        messages.success(
            request,
            'Your request has been received. The customer service team will get back to you shortly.',
        )
        return redirect('customer_portal_home', token=self.token.token)

    def _create_service_ticket(self, portal_req):
        """Create a ServiceTicket. If the staff app isn't installed, return None."""
        from apps.customer_service.models import ServiceTicket

        # Resolve the linked customer_service.Customer via the contact row.
        customer = portal_req.contact.customer if portal_req.contact else None
        if customer is None:
            raise RuntimeError('Cannot create ticket — no linked Customer for contact')

        ticket_type_map = {
            'support': 'support',
            'on_call': 'maintenance',
            'followup': 'support',
            'dispute': 'complaint',
            'question': 'general',
        }

        ticket = ServiceTicket.objects.create(
            customer=customer,
            ticket_type=ticket_type_map.get(portal_req.kind, 'support'),
            priority=portal_req.priority,
            status='new',
            subject=portal_req.subject,
            description=(
                f'Submitted via customer portal by {portal_req.contact.full_name} '
                f'<{portal_req.contact.email}>.\n\n'
                f'{portal_req.description}'
            ),
            requires_feedback=True,
        )

        # Attach contract FK if the migration to add it has been applied.
        if hasattr(ticket, 'contract_id'):
            try:
                ticket.contract = portal_req.contract
                ticket.save(update_fields=['contract'])
            except Exception:
                logger.warning('customer_portal: ticket.contract assign failed')

        return ticket


def _notify_cs_of_new_request(ticket, portal_req):
    try:
        from apps.customer_service.notifications import (
            _send_inapp, heads_of_customer_service,
        )
        for u in heads_of_customer_service():
            _send_inapp(
                u,
                title='New customer-portal request',
                body=(
                    f'{portal_req.contact.full_name} submitted a request via the portal:\n\n'
                    f'{portal_req.subject}'
                ),
                url=f'/customer-service/tickets/{ticket.pk}/',
                kind='customer_portal',
            )
    except Exception:
        logger.exception('customer_portal: CS notification failed')


def _client_ip(request) -> str | None:
    xf = request.META.get('HTTP_X_FORWARDED_FOR', '')
    if xf:
        return xf.split(',')[0].strip() or None
    return request.META.get('REMOTE_ADDR') or None


# ─────────────────────────────────────────────────────────────────────────────
# Visit detail (read-only)
# ─────────────────────────────────────────────────────────────────────────────

class VisitDetailView(TokenRequiredMixin, TemplateView):
    template_name = 'customer_portal/visit_detail.html'

    def get_context_data(self, visit_id, **kwargs):
        ctx = super().get_context_data(**kwargs)

        # SECURITY: filter by both pk AND contract so the URL can't be
        # used to peek at another customer's visit.
        visit = get_object_or_404(
            MaintenanceVisit, pk=visit_id, contract=self.contract,
        )
        ctx['visit'] = visit
        ctx['contract'] = self.contract
        ctx['contact'] = self.contact
        ctx['token'] = self.token
        ctx['on_site_events'] = visit.task.on_site_events.all() if visit.task_id else []
        return ctx


# ─────────────────────────────────────────────────────────────────────────────
# Dispute an on-site event
# ─────────────────────────────────────────────────────────────────────────────

class DisputeOnSiteView(TokenRequiredMixin, View):
    """
    "The tech said they were on site but never showed up."

    Records an OnSiteEvent of type='dispute', flips the linked visit (if
    any) to status='disputed', and fires an in-app alert to CS owner +
    CEO + Admin + Sales head.
    """
    http_method_names = ['post']

    def post(self, request, event_id, **kwargs):
        # The customer disputes a SPECIFIC on_site event — load it filtered
        # by their contract.
        original = get_object_or_404(
            OnSiteEvent, pk=event_id, contract=self.contract,
        )
        if original.event_type != 'arrive':
            return HttpResponseForbidden('Only an "arrived" event can be disputed.')

        note = (request.POST.get('note') or '').strip()[:1000]

        dispute = OnSiteEvent.objects.create(
            task=original.task,
            assignment=original.assignment,
            contract=self.contract,
            event_type='dispute',
            actor_contact=self.contact,
            note=note or 'Customer reports the technician did not show up.',
        )

        # Update the linked maintenance visit, if any.
        visit = MaintenanceVisit.objects.filter(task=original.task).first()
        if visit:
            visit.status = 'disputed'
            visit.save(update_fields=['status', 'updated_at'])

        # Notify staff. We pull recipients via the existing CS helper.
        transaction.on_commit(lambda: _notify_dispute(dispute, original))

        messages.success(
            request,
            'Thank you. Our customer service team has been alerted and will follow up with you shortly.',
        )
        return redirect('customer_portal_home', token=self.token.token)


def _notify_dispute(dispute_event, original_event):
    try:
        from . import notifications
        from apps.customer_service.notifications import heads_of_customer_service, heads_of_sales
        from django.contrib.auth import get_user_model

        User = get_user_model()
        recipients = set()

        # CS heads + sales heads
        for u in heads_of_customer_service() + heads_of_sales():
            recipients.add(u)

        # CEO + Admin (via group name)
        for u in User.objects.filter(
            is_active=True, groups__name__in=['CEO', 'Admin', 'Office Manager'],
        ):
            recipients.add(u)

        # Whoever owns the linked ticket assignment
        try:
            assn = original_event.assignment
            if assn and assn.assigned_to_id:
                recipients.add(assn.assigned_to)
            if assn and assn.assigned_by_id:
                recipients.add(assn.assigned_by)
        except Exception:
            pass

        notifications.send_dispute_notice(event=dispute_event, internal_recipients=recipients)
    except Exception:
        logger.exception('customer_portal: dispute notify failed')


# ─────────────────────────────────────────────────────────────────────────────
# Token-recovery: customer asks for a new link
# ─────────────────────────────────────────────────────────────────────────────

class RequestNewLinkView(View):
    """
    Reached from the 'invalid_token.html' page when someone clicks an
    expired/revoked link. We don't take a token here — just an email.

    To prevent enumeration, we always show the same success message even
    if the email isn't found. Internally, we email the heads of CS so they
    can reissue manually (we don't auto-issue a new token because that
    would let anyone with an email address pivot to portal access).
    """
    http_method_names = ['get', 'post']

    def get(self, request):
        return render(request, 'customer_portal/request_new_link.html')

    def post(self, request):
        email = (request.POST.get('email') or '').strip().lower()
        # Never disclose whether the email is on file.
        try:
            self._notify_cs(email)
        except Exception:
            logger.exception('customer_portal: request-new-link CS notify failed')
        messages.success(
            request,
            'If your email is on file, our customer service team will get back to you with a fresh link.',
        )
        return render(request, 'customer_portal/request_new_link.html', {'submitted': True})

    def _notify_cs(self, email):
        from apps.customer_service.notifications import (
            _send_inapp, heads_of_customer_service,
        )
        if not email:
            return
        for u in heads_of_customer_service():
            _send_inapp(
                u,
                title='Customer requests new portal link',
                body=f'A customer requested a new portal link for: {email}',
                url='/admin/customer_portal/contractcontact/',
                kind='customer_portal',
            )
