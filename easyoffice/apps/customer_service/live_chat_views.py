"""apps/customer_service/live_chat_views.py
HTTP views for opening/closing live chat and rendering customer token page.
"""
from __future__ import annotations

import logging

from django.contrib import messages
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.generic import View

from .models import ServiceTicket, LiveChatSession
from .permissions import CustomerServiceAccessMixin

logger = logging.getLogger(__name__)


def get_live_chat_context(ticket) -> dict:
    """Merge this into TicketDetailView context."""
    try:
        session = LiveChatSession.objects.filter(ticket=ticket).first()
        return {
            'live_chat_session': session,
            'live_chat_messages': (
                session.messages.select_related('agent').order_by('created_at')[:80]
                if session else []
            ),
            'live_chat_ws_path': f'/ws/cs/live-chat/agent/{ticket.pk}/',
        }
    except Exception:
        logger.exception('Could not build live chat context')
        return {'live_chat_session': None, 'live_chat_messages': [], 'live_chat_ws_path': ''}


class LiveChatToggleView(CustomerServiceAccessMixin, View):
    http_method_names = ['post']

    def post(self, request, pk):
        ticket = get_object_or_404(ServiceTicket, pk=pk)
        action = (request.POST.get('action') or '').strip().lower()

        if ticket.status in {'closed', 'cancelled'} and action == 'on':
            messages.error(request, 'You can’t open a live chat on a closed or cancelled ticket.')
            return redirect('customer_service_ticket_detail', pk=ticket.pk)

        try:
            from . import live_chat_services as lcs
            if action == 'on':
                session = lcs.activate_session(ticket, actor=request.user, request=request, send_email=True)
                customer_email = lcs._safe_customer_email(ticket) or '(no email on file)'
                messages.success(
                    request,
                    f'Live chat opened. A tokenised link has been emailed to {customer_email}. '
                    f'The link is bound to the first device that opens it.',
                )
            elif action == 'off':
                lcs.deactivate_session(ticket, actor=request.user, reason='manually_off')
                messages.success(request, 'Live chat closed. The customer link no longer works.')
            else:
                messages.error(request, 'Unknown live-chat action.')
        except Exception as exc:
            logger.exception('LiveChatToggleView failed for ticket %s', ticket.pk)
            messages.error(request, f'Could not toggle live chat: {exc}')

        return redirect('customer_service_ticket_detail', pk=ticket.pk)


class LiveChatResendInviteView(CustomerServiceAccessMixin, View):
    http_method_names = ['post']

    def post(self, request, pk):
        ticket = get_object_or_404(ServiceTicket, pk=pk)
        try:
            from . import live_chat_services as lcs
            session = lcs.get_or_create_session(ticket)
            if not session.is_active:
                messages.warning(request, 'Live chat is OFF. Turn it ON to issue a fresh tokenised link.')
                return redirect('customer_service_ticket_detail', pk=ticket.pk)
            lcs.send_invite_email(session, request=request)
            messages.success(request, f'Invite re-sent to {lcs._safe_customer_email(ticket) or "(no email)"}.')
        except Exception as exc:
            logger.exception('LiveChatResendInviteView failed')
            messages.error(request, f'Could not resend invite: {exc}')
        return redirect('customer_service_ticket_detail', pk=ticket.pk)


class LiveChatCustomerView(View):
    """Anonymous tokenised customer chat page."""
    http_method_names = ['get', 'post']

    @classmethod
    def as_view(cls, **kwargs):
        from django.views.decorators.csrf import csrf_exempt
        return csrf_exempt(super().as_view(**kwargs))

    def get(self, request, token):
        from . import live_chat_services as lcs

        session = (
            LiveChatSession.objects
            .select_related('ticket', 'ticket__customer')
            .filter(token=token)
            .first()
        )
        if session is None:
            return _render_locked(request, 'unknown')

        cookie_in = request.COOKIES.get(lcs.MACHINE_COOKIE_NAME, '')
        issue_cookie = False
        cookie_to_set = cookie_in

        if not session.machine_cookie:
            # First ever visit: bind this device.
            cookie_to_set = cookie_in or lcs.new_machine_cookie_value()
            issue_cookie = True
            ok, reason = lcs.bind_first_visit(
                session,
                cookie_value=cookie_to_set,
                fingerprint_raw='',
                ip=_client_ip(request),
                user_agent=request.META.get('HTTP_USER_AGENT', ''),
            )
            if not ok:
                return _render_locked(request, reason)
        else:
            # Already bound. Validate the incoming cookie (if any). After
            # Fix 1, a missing cookie no longer locks — bind_first_visit
            # returns ok=True and we simply re-issue the stored cookie so
            # the browser heals its binding (e.g. after the cookie path was
            # previously too narrow, or the jar was cleared).
            ok, reason = lcs.bind_first_visit(
                session,
                cookie_value=cookie_in,
                fingerprint_raw='',
                ip=_client_ip(request),
                user_agent=request.META.get('HTTP_USER_AGENT', ''),
            )
            if not ok:
                return _render_locked(request, reason)
            # Re-affirm the canonical cookie on every GET.
            if session.machine_cookie and cookie_in != session.machine_cookie:
                cookie_to_set = session.machine_cookie
                issue_cookie = True

        ok, reason = lcs.session_is_usable(session)
        if not ok:
            return _render_locked(request, reason)

        response = render(request, 'customer_service/live_chat_customer.html', {
            'session': session,
            'ticket': session.ticket,
            'customer': session.ticket.customer,
            'ws_path': f'/ws/cs/live-chat/customer/{session.token}/',
            'finalize_url': request.path,
            'messages': session.messages.select_related('agent').order_by('created_at')[:80],
        })
        if issue_cookie:
            _set_machine_cookie(response, request, lcs.MACHINE_COOKIE_NAME, cookie_to_set)
        return response

    def post(self, request, token):
        from . import live_chat_services as lcs

        session = LiveChatSession.objects.filter(token=token).first()
        if session is None:
            return JsonResponse({'ok': False, 'reason': 'unknown'}, status=404)

        cookie_in = request.COOKIES.get(lcs.MACHINE_COOKIE_NAME, '')
        fp_raw = (request.POST.get('fingerprint') or '').strip()[:512]
        ok, reason = lcs.bind_first_visit(
            session,
            cookie_value=cookie_in,
            fingerprint_raw=fp_raw,
            ip=_client_ip(request),
            user_agent=request.META.get('HTTP_USER_AGENT', ''),
        )
        response = JsonResponse({'ok': ok, 'reason': reason})

        # If the browser cleared cookies but fingerprint matched, re-issue the original cookie.
        if ok and not cookie_in and session.machine_cookie:
            _set_machine_cookie(response, request, lcs.MACHINE_COOKIE_NAME, session.machine_cookie)
        return response


def _set_machine_cookie(response, request, name: str, value: str):
    response.set_cookie(
        name,
        value,
        max_age=60 * 60 * 24 * 30,
        # Scope to the whole site, NOT request.path. The same machine cookie
        # must be sent on the customer page GET/POST *and* on the WebSocket
        # handshake at /ws/cs/live-chat/customer/<token>/. Scoping to the
        # narrow token path meant the browser never returned the cookie on
        # those other requests, which then read as a "different machine".
        path='/',
        httponly=True,
        samesite='Lax',
        secure=request.is_secure(),
    )


def _client_ip(request) -> str:
    xff = request.META.get('HTTP_X_FORWARDED_FOR', '')
    if xff:
        return xff.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR', '') or ''


_LOCK_LABELS = {
    'different_machine': 'This chat link has already been opened on a different device.',
    'expired': 'This chat link has expired.',
    'manually_off': 'The chat has been closed by our team.',
    'ticket_closed': 'The related ticket has been closed.',
    'unknown': 'This link is not recognised.',
    'device_not_bound': 'This device is not authorised for this chat link.',
}


def _render_locked(request, reason):
    return render(
        request,
        'customer_service/live_chat_locked.html',
        {
            'reason': reason or 'manually_off',
            'reason_label': _LOCK_LABELS.get(reason, 'This chat link is no longer active.'),
        },
        status=410 if reason in {'different_machine', 'expired', 'ticket_closed'} else 404 if reason == 'unknown' else 403,
    )
