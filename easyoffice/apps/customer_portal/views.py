"""
apps/customer_portal/views.py
==============================

All portal views are TOKEN-GATED, not login-gated. The token in the URL
is the only thing that authenticates the customer. Therefore EVERY view
that touches portal data must:

  1. Resolve the token via the gate
  2. Check token.is_active
  3. Verify the device binding (OTP-gated for new machines)
  4. Use the token's contract — never trust contract_id from the URL or
     POST body (an attacker with one valid token shouldn't be able to
     pivot to a different contract by editing form fields)

The two-stage gate
------------------
TokenRequiredMixin runs first and resolves token + contract + contact.
DeviceGuardedMixin then resolves the device binding and either lets the
request through OR redirects to the OTP verification page.

DeviceGuardedMixin is composed onto every view EXCEPT the OTP views
themselves (which obviously must run before verification has happened).
"""
from __future__ import annotations

import logging

from django.conf import settings
from django.contrib import messages
from django.db import transaction
from django.http import (
    Http404, HttpResponse, HttpResponseForbidden, FileResponse,
)
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse, NoReverseMatch
from django.utils import timezone
from django.views.generic import View, TemplateView

from . import device_binding as devbind
from . import sync as portal_sync
from .forms import (
    CancelTicketForm, CommentForm, DeviceOTPForm,
    RatingForm, ReopenForm,
)
from .models import (
    ContractContact, DeviceBinding, MaintenanceSchedule,
    MaintenanceVisit, OnSiteEvent, PortalAccessToken,
    PortalSupportRequest, ReopenRequest, TicketAttachment,
    TicketComment, TicketRating,
)

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Token gate
# ─────────────────────────────────────────────────────────────────────────────

class TokenRequiredMixin:
    """
    Resolve self.token / self.contact / self.contract or render a 'token
    invalid / expired' page. Must come BEFORE View in the MRO.
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
        return getattr(settings, 'SUPPORT_EMAIL', None) or 'support@example.com'


# ─────────────────────────────────────────────────────────────────────────────
# Device gate
# ─────────────────────────────────────────────────────────────────────────────

class DeviceGuardedMixin:
    """
    Must come AFTER TokenRequiredMixin in MRO so self.token is set when
    we run. If the device hasn't been verified for this token, redirect
    to VerifyDeviceView. Otherwise stash the resolved DeviceState on
    self.device_state and let the request through.

    Subclasses that legitimately bypass device verification (only the
    OTP view itself) override `_skip_device_check` to True.
    """
    _skip_device_check: bool = False

    def dispatch(self, request, *args, **kwargs):
        # The MRO has TokenRequiredMixin → DeviceGuardedMixin → View.
        # TokenRequiredMixin.dispatch sets self.token / self.contact /
        # self.contract OR returns an early invalid-token response, then
        # calls super().dispatch — which lands here. By this point we
        # have self.token (or we never got here at all).
        if not self._skip_device_check:
            state = devbind.resolve_device_state(self.token, request)
            self.device_state = state
            if not state.is_verified:
                # Stash the original URL so we can land them back where
                # they were aiming after they verify.
                request.session['portal_post_verify_redirect'] = request.get_full_path()
                return redirect('customer_portal_verify_device',
                                token=self.token.token)
        return super().dispatch(request, *args, **kwargs)


# ─────────────────────────────────────────────────────────────────────────────
# Portal home
# ─────────────────────────────────────────────────────────────────────────────

class PortalHomeView(TokenRequiredMixin, DeviceGuardedMixin, TemplateView):
    template_name = 'customer_portal/home.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        contract = self.contract

        all_requests = (
            PortalSupportRequest.objects
            .filter(contract=contract)
            .order_by('-created_at')
        )
        ctx['open_requests'] = [
            r for r in all_requests if r.status in PortalSupportRequest.OPEN_STATUSES
        ][:20]
        ctx['resolved_requests'] = [
            r for r in all_requests if r.status in PortalSupportRequest.RESOLVED_STATUSES
        ][:10]
        ctx['recent_requests'] = list(all_requests[:30])

        ctx['pm_schedule'] = getattr(contract, 'pm_schedule', None)

        visits = (
            MaintenanceVisit.objects
            .filter(contract=contract)
            .select_related('technician', 'task')
            .order_by('-scheduled_for', '-created_at')
        )
        ctx['upcoming_visits'] = [v for v in visits if v.status in ('scheduled', 'dispatched')][:10]
        ctx['recent_visits'] = [v for v in visits if v.status in ('on_site', 'completed', 'verified')][:10]

        try:
            from apps.customer_service.models import ServiceTicket
            ctx['tickets'] = (
                ServiceTicket.objects
                .filter(contract=contract)
                .order_by('-created_at')[:20]
            )
        except Exception:
            ctx['tickets'] = []

        ctx['contact'] = self.contact
        ctx['contract'] = contract
        ctx['token'] = self.token

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

class NewSupportRequestView(TokenRequiredMixin, DeviceGuardedMixin, View):
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

        portal_req = PortalSupportRequest.objects.create(
            contract=self.contract,
            contact=self.contact,
            token_used=self.token,
            kind=kind if kind in dict(PortalSupportRequest.REQUEST_KINDS) else 'support',
            priority=priority if priority in dict(PortalSupportRequest.PRIORITIES) else 'medium',
            subject=subject[:220],
            description=description,
            status='pending_review',
            submitted_from_ip=devbind._client_ip(request),
        )

        try:
            ticket = self._create_service_ticket(portal_req)
            portal_req.service_ticket = ticket
            portal_req.status = 'open'
            portal_req.save(update_fields=['service_ticket', 'status', 'updated_at'])
        except Exception:
            logger.exception('customer_portal: failed to spin up service ticket from portal request')
            ticket = None

        # Seed the conversation with the customer's original description.
        TicketComment.objects.create(
            request=portal_req,
            author_kind='customer',
            author_contact=self.contact,
            author_display_name=self.contact.full_name,
            body=description,
            submitted_from_ip=devbind._client_ip(request),
        )

        if ticket is not None:
            transaction.on_commit(lambda: _notify_cs_of_new_request(ticket, portal_req))

        messages.success(request, 'Your request has been submitted. We\'ll get back to you shortly.')
        return redirect('customer_portal_ticket_detail',
                        token=self.token.token, request_id=portal_req.id)

    def _create_service_ticket(self, portal_req):
        from apps.customer_service.models import ServiceTicket
        customer = portal_req.contact.customer if portal_req.contact else None
        if not customer:
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


# ─────────────────────────────────────────────────────────────────────────────
# Ticket detail (the big one)
# ─────────────────────────────────────────────────────────────────────────────

class TicketDetailView(TokenRequiredMixin, DeviceGuardedMixin, TemplateView):
    template_name = 'customer_portal/ticket_detail.html'

    def _get_request_or_404(self, request_id):
        return get_object_or_404(
            PortalSupportRequest.objects
                .select_related('service_ticket', 'contact')
                .prefetch_related('comments', 'attachments', 'reopen_requests'),
            pk=request_id,
            contract=self.contract,  # contract scope!
        )

    def get(self, request, request_id, **kwargs):
        ticket = self._get_request_or_404(request_id)
        ctx = self._build_context(ticket)
        return render(request, self.template_name, ctx)

    def _build_context(self, ticket: PortalSupportRequest) -> dict:
        # Sync agent / technician names from the linked ServiceTicket if
        # we can — gives the customer the freshest info without us having
        # to background-poll it. Cheap defensive read.
        agent_name, tech_name = self._derive_assignment_names(ticket)
        if (agent_name and agent_name != ticket.assigned_agent_name) \
                or (tech_name and tech_name != ticket.assigned_technician_name):
            try:
                ticket.assigned_agent_name = agent_name or ticket.assigned_agent_name
                ticket.assigned_technician_name = tech_name or ticket.assigned_technician_name
                ticket.save(update_fields=[
                    'assigned_agent_name', 'assigned_technician_name', 'updated_at',
                ])
            except Exception:
                logger.exception('customer_portal: assignment-name sync failed')

        comments = ticket.comments.select_related(
            'author_contact', 'author_user',
        ).prefetch_related('attachments').order_by('created_at')

        return {
            'token': self.token,
            'contract': self.contract,
            'contact': self.contact,
            'ticket': ticket,
            'comments': comments,
            'attachments': ticket.attachments.all().order_by('-created_at'),
            'reopen_requests': ticket.reopen_requests.all().order_by('-created_at'),
            'rating': getattr(ticket, 'rating', None),
            'agent_name': agent_name or ticket.assigned_agent_name,
            'technician_name': tech_name or ticket.assigned_technician_name,
            'comment_form': CommentForm(),
            'cancel_form': CancelTicketForm(),
            'reopen_form': ReopenForm(),
            'rating_form': RatingForm(),
        }

    @staticmethod
    def _derive_assignment_names(ticket: PortalSupportRequest):
        """Best-effort lookup of agent + technician for the linked ServiceTicket."""
        agent = ''
        tech = ''
        st = ticket.service_ticket
        if not st:
            return agent, tech
        try:
            # Conventional fields — fall back to None silently if absent.
            owner = getattr(st, 'assigned_to', None) or getattr(st, 'owner', None)
            if owner:
                agent = getattr(owner, 'full_name', None) or owner.get_username()
        except Exception:
            pass
        try:
            mv = ticket.contract.maintenance_visits.filter(ticket=st).order_by('-created_at').first()
            if mv and mv.technician:
                tech = getattr(mv.technician, 'full_name', None) or mv.technician.get_username()
        except Exception:
            pass
        return agent, tech


# ─────────────────────────────────────────────────────────────────────────────
# Comment on a ticket
# ─────────────────────────────────────────────────────────────────────────────

class TicketCommentView(TokenRequiredMixin, DeviceGuardedMixin, View):
    def post(self, request, request_id, **kwargs):
        ticket = get_object_or_404(
            PortalSupportRequest, pk=request_id, contract=self.contract,
        )
        if not ticket.can_comment:
            messages.error(request, 'This ticket is closed and can no longer accept comments.')
            return redirect('customer_portal_ticket_detail',
                            token=self.token.token, request_id=ticket.id)

        form = CommentForm(request.POST, request.FILES)
        if not form.is_valid():
            for err in form.non_field_errors():
                messages.error(request, err)
            for field, errs in form.errors.items():
                for err in errs:
                    if field != '__all__':
                        messages.error(request, err)
            return redirect('customer_portal_ticket_detail',
                            token=self.token.token, request_id=ticket.id)

        body = form.cleaned_data['body']
        files = form.cleaned_data.get('attachment_files') or []

        with transaction.atomic():
            comment = TicketComment.objects.create(
                request=ticket,
                author_kind='customer',
                author_contact=self.contact,
                author_display_name=self.contact.full_name,
                body=body,
                submitted_from_ip=devbind._client_ip(request),
            )
            for f in files:
                TicketAttachment.objects.create(
                    request=ticket,
                    comment=comment,
                    file=f,
                    original_name=f.name[:200],
                    content_type=getattr(f, 'content_type', '') or '',
                    size_bytes=f.size or 0,
                    uploader_kind='customer',
                    uploader_contact=self.contact,
                )

        transaction.on_commit(lambda: _notify_staff_of_customer_comment(ticket, comment))

        # Mirror the customer's reply onto the staff-side activity log so
        # agents working in the CS app see it alongside their own notes.
        try:
            portal_sync.mirror_customer_comment_to_staff(comment)
        except Exception:
            logger.warning(
                'customer_portal: comment mirror to staff failed',
                exc_info=True,
            )

        messages.success(request, 'Your reply has been added.')
        return redirect('customer_portal_ticket_detail',
                        token=self.token.token, request_id=ticket.id)


# ─────────────────────────────────────────────────────────────────────────────
# Cancel a ticket
# ─────────────────────────────────────────────────────────────────────────────

class TicketCancelView(TokenRequiredMixin, DeviceGuardedMixin, View):
    def post(self, request, request_id, **kwargs):
        ticket = get_object_or_404(
            PortalSupportRequest, pk=request_id, contract=self.contract,
        )
        if not ticket.can_cancel:
            messages.error(request, 'This ticket can no longer be cancelled.')
            return redirect('customer_portal_ticket_detail',
                            token=self.token.token, request_id=ticket.id)

        form = CancelTicketForm(request.POST)
        if not form.is_valid():
            for field, errs in form.errors.items():
                for err in errs:
                    messages.error(request, err)
            return redirect('customer_portal_ticket_detail',
                            token=self.token.token, request_id=ticket.id)

        reason = form.cleaned_data['reason']
        now = timezone.now()

        with transaction.atomic():
            ticket.status = 'cancelled'
            ticket.cancelled_at = now
            ticket.cancellation_reason = reason
            ticket.save(update_fields=[
                'status', 'cancelled_at', 'cancellation_reason', 'updated_at',
            ])

            TicketComment.objects.create(
                request=ticket,
                author_kind='system',
                author_display_name='System',
                event_tag='cancelled',
                body=f'Ticket cancelled by {self.contact.full_name}. Reason: {reason}',
            )

            # Mirror to staff side: updates ServiceTicket.status AND drops a
            # ServiceTicketUpdate row so the cancellation appears in the
            # staff activity log alongside the agent's own notes.
            try:
                portal_sync.push_portal_status_to_staff(
                    ticket, reason=reason, actor=None,
                )
            except Exception:
                logger.warning(
                    'customer_portal: could not mirror cancel to staff side',
                    exc_info=True,
                )

        transaction.on_commit(lambda: _notify_staff_of_cancel(ticket, reason))

        messages.success(request, 'Your ticket has been cancelled. Thank you for letting us know.')
        return redirect('customer_portal_ticket_detail',
                        token=self.token.token, request_id=ticket.id)


# ─────────────────────────────────────────────────────────────────────────────
# Reopen request
# ─────────────────────────────────────────────────────────────────────────────

class TicketReopenView(TokenRequiredMixin, DeviceGuardedMixin, View):
    def post(self, request, request_id, **kwargs):
        ticket = get_object_or_404(
            PortalSupportRequest, pk=request_id, contract=self.contract,
        )
        if not ticket.can_request_reopen:
            messages.error(request, 'This ticket isn\'t eligible to be reopened.')
            return redirect('customer_portal_ticket_detail',
                            token=self.token.token, request_id=ticket.id)

        # Don't let a customer spam reopen requests — one pending at a time.
        existing_pending = ticket.reopen_requests.filter(decision='pending').exists()
        if existing_pending:
            messages.info(request, 'You already have a reopen request pending review for this ticket.')
            return redirect('customer_portal_ticket_detail',
                            token=self.token.token, request_id=ticket.id)

        form = ReopenForm(request.POST)
        if not form.is_valid():
            for field, errs in form.errors.items():
                for err in errs:
                    messages.error(request, err)
            return redirect('customer_portal_ticket_detail',
                            token=self.token.token, request_id=ticket.id)

        reason = form.cleaned_data['reason']

        with transaction.atomic():
            reopen = ReopenRequest.objects.create(
                request=ticket,
                requested_by_contact=self.contact,
                reason=reason,
                decision='pending',
            )
            TicketComment.objects.create(
                request=ticket,
                author_kind='system',
                author_display_name='System',
                event_tag='reopen_requested',
                body=f'{self.contact.full_name} requested this ticket be reopened: {reason}',
            )

            # Drop a row on the staff activity log so agents see the
            # reopen request inline with their other ticket activity.
            # We DON'T flip ServiceTicket.status — staff approval does that.
            if ticket.service_ticket:
                try:
                    portal_sync._log_to_staff_activity(
                        ticket.service_ticket,
                        update_type='customer_update',
                        body=(
                            f'↺ Reopen requested by {self.contact.full_name} '
                            f'via the portal.\n\nReason: {reason}\n\n'
                            f'(Awaiting staff review — see ReopenRequest admin.)'
                        ),
                        user=None,
                    )
                except Exception:
                    logger.warning(
                        'customer_portal: could not log reopen to staff side',
                        exc_info=True,
                    )

        transaction.on_commit(lambda: _notify_staff_of_reopen(ticket, reopen))

        messages.success(
            request,
            'We\'ve received your request to reopen this ticket — our team will review and respond shortly.',
        )
        return redirect('customer_portal_ticket_detail',
                        token=self.token.token, request_id=ticket.id)


# ─────────────────────────────────────────────────────────────────────────────
# Rate a ticket
# ─────────────────────────────────────────────────────────────────────────────

class TicketRateView(TokenRequiredMixin, DeviceGuardedMixin, View):
    def post(self, request, request_id, **kwargs):
        ticket = get_object_or_404(
            PortalSupportRequest, pk=request_id, contract=self.contract,
        )
        if not ticket.can_rate:
            messages.info(request, 'You\'ve already rated this ticket — thank you!')
            return redirect('customer_portal_ticket_detail',
                            token=self.token.token, request_id=ticket.id)

        form = RatingForm(request.POST)
        if not form.is_valid():
            for field, errs in form.errors.items():
                for err in errs:
                    messages.error(request, err)
            return redirect('customer_portal_ticket_detail',
                            token=self.token.token, request_id=ticket.id)

        score = form.cleaned_data['score']
        feedback = (form.cleaned_data.get('feedback') or '').strip()
        recommend = form.cleaned_data.get('would_recommend')

        with transaction.atomic():
            TicketRating.objects.create(
                request=ticket,
                rated_by_contact=self.contact,
                score=score,
                feedback=feedback,
                would_recommend=recommend,
                submitted_from_ip=devbind._client_ip(request),
            )
            # Rating implicitly closes the ticket (resolved → closed).
            status_changed = False
            if ticket.status == 'resolved':
                ticket.status = 'closed'
                ticket.closed_at = timezone.now()
                ticket.save(update_fields=['status', 'closed_at', 'updated_at'])
                status_changed = True

            TicketComment.objects.create(
                request=ticket,
                author_kind='system',
                author_display_name='System',
                event_tag='rated',
                body=f'{self.contact.full_name} rated this resolution {score}★.'
                     + (f' Feedback: {feedback}' if feedback else ''),
            )

            # Mirror to staff side. Two things show up:
            #  1. The rating itself (always) — so agents see the score.
            #  2. The status change to 'closed' — only if rating closed it.
            if ticket.service_ticket:
                rating_body = (
                    f'⭐ Customer rated the resolution {score}/5 via the portal.'
                    + (f'\n\nFeedback: {feedback}' if feedback else '')
                    + (
                        f'\n\nWould recommend: '
                        + ('yes' if recommend is True
                           else 'no' if recommend is False
                           else 'no answer')
                    )
                )
                try:
                    portal_sync._log_to_staff_activity(
                        ticket.service_ticket,
                        update_type='customer_update',
                        body=rating_body,
                        user=None,
                    )
                except Exception:
                    logger.warning(
                        'customer_portal: could not log rating to staff side',
                        exc_info=True,
                    )

                if status_changed:
                    try:
                        portal_sync.push_portal_status_to_staff(
                            ticket, reason='Closed by customer rating', actor=None,
                        )
                    except Exception:
                        logger.warning(
                            'customer_portal: could not mirror close to staff side',
                            exc_info=True,
                        )

        messages.success(request, 'Thank you for your feedback — it helps us get better.')
        return redirect('customer_portal_ticket_detail',
                        token=self.token.token, request_id=ticket.id)


# ─────────────────────────────────────────────────────────────────────────────
# Attachment download (token + contract + device gated)
# ─────────────────────────────────────────────────────────────────────────────

class AttachmentDownloadView(TokenRequiredMixin, DeviceGuardedMixin, View):
    def get(self, request, request_id, attachment_id, **kwargs):
        attachment = get_object_or_404(
            TicketAttachment.objects.select_related('request'),
            pk=attachment_id,
            request_id=request_id,
            request__contract=self.contract,
        )
        try:
            f = attachment.file.open('rb')
        except FileNotFoundError:
            raise Http404('File no longer available.')
        resp = FileResponse(
            f,
            as_attachment=True,
            filename=attachment.original_name,
            content_type=attachment.content_type or 'application/octet-stream',
        )
        return resp


# ─────────────────────────────────────────────────────────────────────────────
# Visit detail (read-only)
# ─────────────────────────────────────────────────────────────────────────────

class VisitDetailView(TokenRequiredMixin, DeviceGuardedMixin, TemplateView):
    template_name = 'customer_portal/visit_detail.html'

    def get_context_data(self, visit_id, **kwargs):
        ctx = super().get_context_data(**kwargs)
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
# Dispute on-site
# ─────────────────────────────────────────────────────────────────────────────

class DisputeOnSiteView(TokenRequiredMixin, DeviceGuardedMixin, View):
    http_method_names = ['post']

    def post(self, request, event_id, **kwargs):
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

        visit = MaintenanceVisit.objects.filter(task=original.task).first()
        if visit:
            visit.status = 'disputed'
            visit.save(update_fields=['status', 'updated_at'])

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

        for u in heads_of_customer_service() + heads_of_sales():
            recipients.add(u)

        for u in User.objects.filter(
            is_active=True, groups__name__in=['CEO', 'Admin', 'Office Manager'],
        ):
            recipients.add(u)

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
# Token-recovery
# ─────────────────────────────────────────────────────────────────────────────

class RequestNewLinkView(View):
    """
    Customer's portal link is dead (expired, revoked, lost) and they want
    a new one. We can't issue the link automatically — that would let
    anyone with an email address farm valid tokens — so we hand it off to
    customer service:

      1. Look up active ContractContact rows matching the email and create
         a ticket per matching contact, attached to the right customer.
      2. If NO active contact matches (off-boarded, typo, never on file),
         we still create ONE generic "needs triage" ticket so the request
         is visible in the CS queue. Staff can then verify and decide.
      3. Notify the heads of customer service in-app, linking to the
         ticket(s). Falls back to superusers if no heads are configured,
         so a fresh deployment doesn't silently swallow requests.

    Crucially, the customer-visible response is IDENTICAL whether or not
    the email is on file ("if your email is on file..."). That's the
    same anti-enumeration behaviour we had before; we're just doing more
    work behind the scenes when it IS on file.
    """
    http_method_names = ['get', 'post']

    def get(self, request):
        return render(request, 'customer_portal/request_new_link.html')

    def post(self, request):
        email = (request.POST.get('email') or '').strip().lower()
        logger.info('customer_portal: request-new-link POST email=%r', email)

        tickets = []
        ticket_creation_error = None
        try:
            tickets = self._create_tickets_for_email(request, email)
        except Exception as exc:
            # Capture the message so we can surface it to CS heads in-app.
            ticket_creation_error = repr(exc)
            logger.exception('customer_portal: request-new-link ticket creation failed')

        # If we found no matching contacts, still create ONE generic
        # triage ticket so the request appears in the CS queue. This is
        # the path that previously silently dropped requests.
        if not tickets and email:
            try:
                fallback = self._create_triage_ticket(request, email)
                if fallback:
                    tickets.append(fallback)
            except Exception as exc:
                ticket_creation_error = ticket_creation_error or repr(exc)
                logger.exception('customer_portal: triage ticket creation failed')

        logger.info(
            'customer_portal: request-new-link finished email=%r tickets_created=%d',
            email, len(tickets),
        )

        try:
            self._notify_cs(email, tickets, error=ticket_creation_error)
        except Exception:
            logger.exception('customer_portal: request-new-link CS notify failed')

        messages.success(
            request,
            'If your email is on file, our customer service team will get back to you with a fresh link.',
        )
        return render(request, 'customer_portal/request_new_link.html', {'submitted': True})

    # ── Ticket creation ────────────────────────────────────────────────
    def _create_tickets_for_email(self, request, email):
        """
        Create a ServiceTicket for every active ContractContact matching
        the email. Returns the list of created tickets (may be empty).

        We deliberately do NOT create a PortalSupportRequest here: there
        is no valid token to anchor it to, and the staff workflow is "CS
        sees a ticket → issues a new portal token from the contact admin
        → emails the customer". The portal-side mirror picks up later
        once the customer uses their new link.
        """
        if not email:
            logger.info('customer_portal: request-new-link with empty email — bailing')
            return []

        # NB: we check both is_active=True AND is_active=False contacts.
        # An off-boarded contact who tries to recover their link is still
        # something CS should know about — they may need to be reinstated,
        # or it may be a fraud signal. We log it either way.
        all_matches = list(
            ContractContact.objects
            .filter(email__iexact=email)
            .select_related('contract', 'customer')
        )
        contacts = [c for c in all_matches if c.is_active]
        inactive = [c for c in all_matches if not c.is_active]

        logger.info(
            'customer_portal: request-new-link lookup email=%r matched=%d active=%d inactive=%d',
            email, len(all_matches), len(contacts), len(inactive),
        )
        if inactive and not contacts:
            logger.warning(
                'customer_portal: request-new-link email=%r matched only INACTIVE contacts (%s) — '
                'falling through to generic triage ticket. Reactivate the contact if appropriate.',
                email, [c.pk for c in inactive],
            )

        if not contacts:
            return []

        try:
            from apps.customer_service.models import ServiceTicket
        except Exception:
            logger.exception('customer_portal: ServiceTicket model not importable')
            return []

        ip = devbind._client_ip(request)
        ua = (request.META.get('HTTP_USER_AGENT') or '')[:300]

        tickets = []
        for contact in contacts:
            if not contact.customer_id:
                logger.warning(
                    'customer_portal: contact %s has no customer; skipping ticket creation',
                    contact.pk,
                )
                continue
            try:
                # NOTE: we deliberately do NOT wrap this in transaction.atomic().
                # If the caller (post()) is itself in an outer transaction —
                # which Django's ATOMIC_REQUESTS would do — a nested atomic
                # block that swallows an exception leaves the outer txn in
                # a broken state and any later ORM call raises
                # "TransactionManagementError: An error occurred in the
                # current transaction. You can't execute queries until..."
                # Single create() call is already atomic at the DB level.
                ticket = self._safe_create_ticket(
                    ServiceTicket,
                    customer=contact.customer,
                    subject=f'Portal access request — {contact.full_name}',
                    description=(
                        f'{contact.full_name} <{contact.email}> requested a new '
                        f'customer-portal access link via the public '
                        f'"Request a new link" page.\n\n'
                        f'Contract: {contact.contract}\n'
                        f'Contact role: {contact.get_role_display()}\n'
                        f'Submitted from IP: {ip or "unknown"}\n'
                        f'User-Agent: {ua or "unknown"}\n\n'
                        f'Action required: verify the request is legitimate '
                        f'(call the contact back on a known number if in doubt), '
                        f'then issue a fresh PortalAccessToken from the contact '
                        f'admin and email it to them.'
                    ),
                    contract=contact.contract,
                )
                if ticket:
                    logger.info(
                        'customer_portal: created ServiceTicket %s for contact %s (email=%r)',
                        ticket.pk, contact.pk, email,
                    )
                    tickets.append(ticket)
            except Exception:
                logger.exception(
                    'customer_portal: failed to create ServiceTicket for contact %s (email=%r)',
                    contact.pk, email,
                )
                continue

        return tickets

    def _create_triage_ticket(self, request, email):
        """
        Fallback: create a single generic ticket when no active contact
        matched. This is the path that previously caused requests to
        silently disappear. The ticket is tagged for triage so CS can
        verify the email and either reactivate a contact or reject the
        request as bogus.
        """
        try:
            from apps.customer_service.models import ServiceTicket
        except Exception:
            logger.exception('customer_portal: ServiceTicket model not importable for triage')
            return None

        ip = devbind._client_ip(request)
        ua = (request.META.get('HTTP_USER_AGENT') or '')[:300]

        # Check if there were inactive matches so we can include them in
        # the description as a hint for the CS agent.
        inactive_hint = ''
        try:
            inactive = list(
                ContractContact.objects
                .filter(email__iexact=email, is_active=False)
                .select_related('contract', 'customer')
            )
            if inactive:
                lines = [
                    f'  • {c.full_name} on contract {c.contract} '
                    f'(customer: {c.customer.display_name if c.customer_id else "—"}, '
                    f'pk={c.pk})'
                    for c in inactive
                ]
                inactive_hint = (
                    '\n\nNOTE: This email matches the following INACTIVE '
                    'contract contacts:\n' + '\n'.join(lines) +
                    '\n\nIf the request is legitimate, reactivate the '
                    'appropriate contact and issue a fresh portal token.'
                )
        except Exception:
            logger.warning('customer_portal: triage inactive lookup failed', exc_info=True)

        return self._safe_create_ticket(
            ServiceTicket,
            customer=None,  # unknown — CS triages
            subject=f'Portal access request — needs triage ({email})',
            description=(
                f'A portal-link request was submitted for "{email}", but '
                f'no active contract contact matches that address.\n\n'
                f'Submitted from IP: {ip or "unknown"}\n'
                f'User-Agent: {ua or "unknown"}\n\n'
                f'Action required: verify whether this email belongs to a '
                f'known customer, reactivate the contact if appropriate, '
                f'and respond to the customer.'
                f'{inactive_hint}'
            ),
            contract=None,
        )

    @staticmethod
    def _safe_create_ticket(ServiceTicket, *, customer, subject, description, contract):
        """
        Create a ServiceTicket while shielding the caller from differences
        in the model schema. If the model requires a non-nullable
        `customer`, we'll detect it and skip rather than raise.

        Returns the ticket on success, or None on failure (failure is
        already logged).
        """
        try:
            kwargs = dict(
                ticket_type='support',
                priority='medium',
                status='new',
                subject=subject[:240],  # most subject fields cap at 255
                description=description,
                requires_feedback=False,
            )
            if customer is not None:
                kwargs['customer'] = customer
            if contract is not None:
                kwargs['contract'] = contract
            return ServiceTicket.objects.create(**kwargs)
        except TypeError:
            # Model probably doesn't have one of the kwargs (e.g. requires_feedback).
            # Retry with only the essentials.
            logger.warning(
                'customer_portal: ServiceTicket.create rejected kwargs; retrying minimal',
                exc_info=True,
            )
            try:
                kwargs = dict(
                    subject=subject[:240],
                    description=description,
                    status='new',
                )
                if customer is not None:
                    kwargs['customer'] = customer
                if contract is not None:
                    kwargs['contract'] = contract
                return ServiceTicket.objects.create(**kwargs)
            except Exception:
                logger.exception('customer_portal: minimal ServiceTicket.create also failed')
                return None
        except Exception:
            logger.exception('customer_portal: ServiceTicket.create raised')
            return None

    # ── Notify CS heads ────────────────────────────────────────────────
    def _notify_cs(self, email, tickets, error=None):
        from apps.customer_service.notifications import (
            _send_inapp, heads_of_customer_service,
        )
        if not email:
            return

        heads = list(heads_of_customer_service())
        if not heads:
            # Fallback: if no heads are configured, alert all superusers
            # so a fresh deployment doesn't silently swallow these
            # requests. Better to be noisy than invisible.
            try:
                from apps.core.models import User as _U
                heads = list(_U.objects.filter(is_superuser=True, is_active=True))
                logger.info(
                    'customer_portal: no CS heads configured — falling back to %d superuser(s)',
                    len(heads),
                )
            except Exception:
                logger.exception('customer_portal: superuser fallback failed')

        if not heads:
            logger.warning(
                'customer_portal: no heads/superusers to notify about portal-link request '
                'email=%r — request will be invisible to staff.',
                email,
            )
            return

        # Compose a body with the error appended if ticket creation broke.
        error_suffix = f' (Note: ticket creation hit an error: {error})' if error else ''

        if tickets:
            # Link directly to the freshly-created ticket(s). When there
            # are multiple, we send one notification per ticket so each
            # one is individually clickable.
            for ticket in tickets:
                try:
                    ticket_url = reverse(
                        'customer_service_ticket_detail',
                        kwargs={'pk': ticket.pk},
                    )
                except NoReverseMatch:
                    # CS app not mounted or URL renamed — degrade to admin.
                    ticket_url = (
                        f'/admin/customer_service/serviceticket/{ticket.pk}/change/'
                    )
                for u in heads:
                    _send_inapp(
                        u,
                        title='Customer requests new portal link',
                        body=(
                            f'{email} requested a new portal link. '
                            f'A ticket has been opened — review and issue '
                            f'a fresh token from the contact admin.'
                            + error_suffix
                        ),
                        url=ticket_url,
                        kind='customer_portal',
                    )
        else:
            # Couldn't even create a fallback triage ticket — surface
            # this hard so someone investigates.
            for u in heads:
                _send_inapp(
                    u,
                    title='⚠️ Portal-link request could NOT create a ticket',
                    body=(
                        f'A portal-link request was submitted for "{email}", '
                        f'but no ticket could be created. Check the server '
                        f'logs and investigate manually.'
                        + error_suffix
                    ),
                    url='/admin/customer_portal/contractcontact/',
                    kind='customer_portal',
                )


# ─────────────────────────────────────────────────────────────────────────────
# Device verification flow (NOT DeviceGuarded — by definition!)
# ─────────────────────────────────────────────────────────────────────────────

class VerifyDeviceView(TokenRequiredMixin, View):
    """
    Sees an unrecognised device for this token. Steps:

      GET → ensure cookie seed exists → if no active OTP for this fp,
            mint one and email it; render verify form.
      POST → validate the 6-digit code; on success, create DeviceBinding
             and redirect to the originally-requested URL (or portal home).
    """
    template_name = 'customer_portal/verify_device.html'

    def _resolve_state(self, request):
        return devbind.resolve_device_state(self.token, request)

    def get(self, request, **kwargs):
        state = self._resolve_state(request)

        # Already verified? Don't loop them through the OTP page.
        if state.is_verified:
            target = request.session.pop('portal_post_verify_redirect', None) \
                or reverse('customer_portal_home', kwargs={'token': self.token.token})
            resp = redirect(target)
            if state.seed_was_new:
                devbind.attach_device_cookie(resp, state.seed)
            return resp

        # No live OTP for this device? Mint one and email.
        from .models import DeviceOTP
        latest = (
            DeviceOTP.objects
            .filter(
                token=self.token,
                fingerprint_hash=state.fingerprint,
                consumed_at__isnull=True,
                invalidated_at__isnull=True,
            )
            .order_by('-issued_at')
            .first()
        )
        already_sent = bool(latest and latest.is_consumable)
        if not already_sent:
            otp, code = devbind.issue_device_otp(
                token=self.token,
                fingerprint=state.fingerprint,
                request=request,
                purpose='device_rebind' if state.binding else 'device_verify',
            )
            self._send_otp_email(otp, code)

        ctx = {
            'token': self.token,
            'contract': self.contract,
            'contact': self.contact,
            'form': DeviceOTPForm(),
            'masked_email': self._mask_email(self.contact.email),
            'is_rebind': bool(state.binding),
            'device_label': devbind.device_label(request),
        }
        resp = render(request, self.template_name, ctx)
        if state.seed_was_new:
            devbind.attach_device_cookie(resp, state.seed)
        return resp

    def post(self, request, **kwargs):
        state = self._resolve_state(request)
        action = request.POST.get('action', 'verify')

        # "Resend code" path
        if action == 'resend':
            otp, code = devbind.issue_device_otp(
                token=self.token,
                fingerprint=state.fingerprint,
                request=request,
                purpose='device_rebind' if state.binding else 'device_verify',
            )
            self._send_otp_email(otp, code)
            messages.success(request, 'A new code has been sent to your email.')
            resp = redirect('customer_portal_verify_device', token=self.token.token)
            if state.seed_was_new:
                devbind.attach_device_cookie(resp, state.seed)
            return resp

        # Verify path
        form = DeviceOTPForm(request.POST)
        if not form.is_valid():
            ctx = {
                'token': self.token,
                'contract': self.contract,
                'contact': self.contact,
                'form': form,
                'masked_email': self._mask_email(self.contact.email),
                'is_rebind': bool(state.binding),
                'device_label': devbind.device_label(request),
            }
            return render(request, self.template_name, ctx, status=400)

        ok, msg, _binding = devbind.verify_device_otp(
            token=self.token,
            fingerprint=state.fingerprint,
            code=form.cleaned_data['code'],
            request=request,
        )
        if not ok:
            messages.error(request, msg)
            resp = redirect('customer_portal_verify_device', token=self.token.token)
            if state.seed_was_new:
                devbind.attach_device_cookie(resp, state.seed)
            return resp

        target = request.session.pop('portal_post_verify_redirect', None) \
            or reverse('customer_portal_home', kwargs={'token': self.token.token})
        messages.success(request, 'Device verified — welcome back.')
        resp = redirect(target)
        if state.seed_was_new:
            devbind.attach_device_cookie(resp, state.seed)
        return resp

    @staticmethod
    def _mask_email(email: str) -> str:
        """alice@acme.com → a***e@acme.com"""
        if not email or '@' not in email:
            return email or ''
        local, domain = email.split('@', 1)
        if len(local) <= 2:
            masked = local[0] + '*'
        else:
            masked = local[0] + '*' * (len(local) - 2) + local[-1]
        return f'{masked}@{domain}'

    def _send_otp_email(self, otp, code: str) -> None:
        """
        Plain text body kept terse on purpose — the customer just needs the
        code, not marketing copy. We try to use the notifications module's
        SMTP helper if available; otherwise fall back to django.core.mail.
        """
        from django.core.mail import EmailMultiAlternatives
        from django.template.loader import render_to_string

        ctx = {
            'code': code,
            'contact': self.contact,
            'contract': self.contract,
            'expires_minutes': int(otp.OTP_TTL.total_seconds() / 60),
            'device_label': devbind.device_label(self.request),
            'requested_ip': otp.requested_ip,
            'is_rebind': otp.purpose == 'device_rebind',
        }

        subject = (
            'Verify a new device for your support portal'
            if ctx['is_rebind']
            else 'Your support portal verification code'
        )

        text_body = render_to_string('customer_portal/email/device_otp.txt', ctx)
        try:
            html_body = render_to_string('customer_portal/email/device_otp.html', ctx)
        except Exception:
            html_body = None

        from_email = (
            getattr(settings, 'DEFAULT_FROM_EMAIL', None)
            or getattr(settings, 'SERVER_EMAIL', None)
            or 'no-reply@example.com'
        )

        msg = EmailMultiAlternatives(
            subject=subject,
            body=text_body,
            from_email=from_email,
            to=[self.contact.email],
        )
        if html_body:
            msg.attach_alternative(html_body, 'text/html')
        try:
            msg.send(fail_silently=False)
        except Exception:
            logger.exception('customer_portal: OTP email send failed')


# ─────────────────────────────────────────────────────────────────────────────
# Staff-side helper notifications
# ─────────────────────────────────────────────────────────────────────────────

def _notify_staff_of_customer_comment(ticket, comment):
    try:
        from apps.customer_service.notifications import (
            _send_inapp, heads_of_customer_service,
        )
        recipients = set(heads_of_customer_service())

        # Also ping whoever owns the linked ServiceTicket.
        st = ticket.service_ticket
        if st:
            owner = getattr(st, 'assigned_to', None) or getattr(st, 'owner', None)
            if owner:
                recipients.add(owner)

        for u in recipients:
            _send_inapp(
                u,
                title='New customer reply',
                body=f'{comment.display_name} replied on "{ticket.subject}".',
                url=(f'/customer-service/tickets/{st.pk}/' if st else f'/admin/customer_portal/portalsupportrequest/{ticket.id}/change/'),
                kind='customer_portal_comment',
            )
    except Exception:
        logger.exception('customer_portal: staff comment notify failed')


def _notify_staff_of_cancel(ticket, reason):
    try:
        from apps.customer_service.notifications import (
            _send_inapp, heads_of_customer_service,
        )
        for u in heads_of_customer_service():
            _send_inapp(
                u,
                title='Customer cancelled a request',
                body=f'"{ticket.subject}" was cancelled by {ticket.contact.full_name if ticket.contact else "a customer"}. Reason: {reason}',
                url=f'/admin/customer_portal/portalsupportrequest/{ticket.id}/change/',
                kind='customer_portal_cancel',
            )
    except Exception:
        logger.exception('customer_portal: cancel notify failed')


def _notify_staff_of_reopen(ticket, reopen):
    try:
        from apps.customer_service.notifications import (
            _send_inapp, heads_of_customer_service,
        )
        recipients = set(heads_of_customer_service())
        st = ticket.service_ticket
        if st:
            owner = getattr(st, 'assigned_to', None) or getattr(st, 'owner', None)
            if owner:
                recipients.add(owner)
        for u in recipients:
            _send_inapp(
                u,
                title='⚠️ Customer requests reopen',
                body=f'{ticket.contact.full_name if ticket.contact else "A customer"} asked to reopen "{ticket.subject}". Reason: {reopen.reason[:200]}',
                url=f'/admin/customer_portal/reopenrequest/{reopen.id}/change/',
                kind='customer_portal_reopen',
            )
    except Exception:
        logger.exception('customer_portal: reopen notify failed')