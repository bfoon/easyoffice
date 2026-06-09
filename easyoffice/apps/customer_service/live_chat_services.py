"""
apps/customer_service/live_chat_services.py
Tokenised live-chat service layer.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from .models import LiveChatSession, LiveChatMessage, ServiceTicket, ServiceTicketUpdate

logger = logging.getLogger(__name__)

MACHINE_COOKIE_BYTES = 24
MACHINE_COOKIE_NAME = 'cs_livechat_mid'
DEFAULT_EXPIRY_DAYS = 14


def _safe_customer_email(ticket: ServiceTicket) -> str:
    customer = getattr(ticket, 'customer', None)
    return (getattr(customer, 'email', '') or getattr(ticket, 'callback_email', '') or '').strip()


def _safe_customer_name(ticket: ServiceTicket) -> str:
    customer = getattr(ticket, 'customer', None)
    return (
        getattr(customer, 'full_name', '')
        or getattr(customer, 'display_name', '')
        or getattr(customer, 'name', '')
        or 'there'
    )


def get_or_create_session(ticket: ServiceTicket) -> LiveChatSession:
    session, _ = LiveChatSession.objects.get_or_create(ticket=ticket)
    return session


@transaction.atomic
def activate_session(ticket: ServiceTicket, *, actor, request=None, send_email: bool = True) -> LiveChatSession:
    """Turn chat ON, rotate token, clear old device binding, and email the customer."""
    session = get_or_create_session(ticket)
    session.regenerate_token()
    session.is_active = True
    session.activated_at = timezone.now()
    session.activated_by = actor if (actor and getattr(actor, 'is_authenticated', False)) else None
    session.expires_at = timezone.now() + timedelta(days=DEFAULT_EXPIRY_DAYS)
    session.save()

    ServiceTicketUpdate.objects.create(
        ticket=ticket,
        user=actor if (actor and getattr(actor, 'is_authenticated', False)) else None,
        update_type='note',
        body='💬 Live chat enabled. New tokenised chat link issued to the customer by email.',
    )

    if send_email:
        transaction.on_commit(lambda: send_invite_email(session, request=request))

    transaction.on_commit(lambda: broadcast_session_state(session, kind='activated'))
    return session


@transaction.atomic
def deactivate_session(ticket: ServiceTicket, *, actor, reason: str = 'manually_off') -> LiveChatSession:
    session = get_or_create_session(ticket)
    session.is_active = False
    session.deactivated_at = timezone.now()
    if reason in {'ticket_closed', 'expired', 'different_machine'}:
        session.is_locked = True
        session.lock_reason = reason
        session.locked_at = timezone.now()
    session.save()

    body_map = {
        'manually_off': '💬 Live chat disabled by CS. Customer link no longer works.',
        'ticket_closed': '💬 Live chat closed because the ticket was closed.',
        'expired': '💬 Live chat link expired.',
        'different_machine': '💬 Live chat locked because another device tried to use the link.',
    }
    ServiceTicketUpdate.objects.create(
        ticket=ticket,
        user=actor if (actor and getattr(actor, 'is_authenticated', False)) else None,
        update_type='note',
        body=body_map.get(reason, f'💬 Live chat deactivated ({reason}).'),
    )

    transaction.on_commit(lambda: broadcast_session_state(session, kind='deactivated'))
    return session


def session_is_usable(session: LiveChatSession) -> tuple[bool, str]:
    if not session.is_active:
        return False, 'manually_off'
    if session.is_locked:
        return False, session.lock_reason or 'different_machine'
    if session.expires_at and timezone.now() > session.expires_at:
        return False, 'expired'
    if session.ticket.status in {'closed', 'cancelled'}:
        return False, 'ticket_closed'
    return True, ''


def new_machine_cookie_value() -> str:
    return secrets.token_urlsafe(MACHINE_COOKIE_BYTES)


def hash_fingerprint(raw: str) -> str:
    if not raw:
        return ''
    key = (getattr(settings, 'SECRET_KEY', '') or '').encode('utf-8')
    return hmac.new(key, raw.encode('utf-8', errors='ignore'), hashlib.sha256).hexdigest()


@transaction.atomic
def bind_first_visit(
    session: LiveChatSession,
    *,
    cookie_value: str,
    fingerprint_raw: str,
    ip: str,
    user_agent: str,
) -> tuple[bool, str]:
    """Bind first browser/device and reject later different devices."""
    fp_hash = hash_fingerprint(fingerprint_raw)

    if not session.machine_cookie:
        session.machine_cookie = cookie_value or new_machine_cookie_value()
        session.machine_fingerprint = fp_hash
        session.first_visit_at = timezone.now()
        session.first_visit_ip = ip or None
        session.first_visit_ua = (user_agent or '')[:400]
        session.save(update_fields=[
            'machine_cookie', 'machine_fingerprint', 'first_visit_at',
            'first_visit_ip', 'first_visit_ua', 'updated_at',
        ])
        return True, ''

    cookie_matches = bool(cookie_value) and cookie_value == session.machine_cookie
    fp_matches = bool(fp_hash) and fp_hash == session.machine_fingerprint

    if cookie_matches or fp_matches:
        # If the cookie matched but we never stored a fingerprint (first
        # visit bound on GET with no fp yet), backfill it now so future
        # POSTs can match on either signal.
        if cookie_matches and fp_hash and not session.machine_fingerprint:
            session.machine_fingerprint = fp_hash
            session.save(update_fields=['machine_fingerprint', 'updated_at'])
        return True, ''

    # ── No positive match. Decide whether this is genuinely a different
    #    device, or just a request that carried no identifying signal
    #    (cookie not sent back yet / fingerprint not collected yet).
    #
    #    We ONLY lock when there is a *positive conflict*: a real cookie
    #    that differs from the stored one. A bare GET/POST that simply
    #    didn't send the cookie back (different path scope, cookie not yet
    #    written, etc.) must NOT lock the session — that was the bug that
    #    locked the customer out the instant they opened their own link.
    cookie_conflict = bool(cookie_value) and cookie_value != session.machine_cookie
    fp_conflict = bool(fp_hash) and bool(session.machine_fingerprint) and fp_hash != session.machine_fingerprint

    if cookie_conflict and fp_conflict:
        # Both strong signals disagree → almost certainly a different device.
        lock_session(session, reason='different_machine', attempt_ip=ip, attempt_ua=user_agent)
        return False, 'different_machine'

    if cookie_conflict and not fp_hash:
        # A different cookie with no fingerprint to corroborate. Could be a
        # shared/cleared cookie jar. Be conservative: lock only if there is
        # no fingerprint binding to fall back on.
        if not session.machine_fingerprint:
            lock_session(session, reason='different_machine', attempt_ip=ip, attempt_ua=user_agent)
            return False, 'different_machine'

    # Otherwise: no conclusive evidence of a second device. Allow, and let
    # the caller re-issue the original cookie so the binding heals itself.
    return True, ''


@transaction.atomic
def lock_session(session: LiveChatSession, *, reason: str, attempt_ip: str = '', attempt_ua: str = '') -> LiveChatSession:
    session.is_active = False
    session.is_locked = True
    session.lock_reason = reason
    session.locked_at = timezone.now()
    session.locked_attempt_ip = attempt_ip or None
    session.locked_attempt_ua = (attempt_ua or '')[:400]
    session.save()

    ServiceTicketUpdate.objects.create(
        ticket=session.ticket,
        user=None,
        update_type='note',
        body=(
            '⚠️ Live chat LOCKED — a second machine tried to open the chat link.\n'
            f'Original visit: {session.first_visit_ip or "—"}\n'
            f'Hijack attempt: {attempt_ip or "—"}\n'
            f'UA: {(attempt_ua or "")[:200]}\n\n'
            'The customer link no longer works. Toggle the chat ON again to issue a fresh link.'
        ),
    )

    transaction.on_commit(lambda: send_hijack_alert(session, attempt_ip, attempt_ua))
    transaction.on_commit(lambda: broadcast_session_state(session, kind='locked'))
    return session


@transaction.atomic
def post_message(session: LiveChatSession, *, author_kind: str, body: str, agent=None) -> LiveChatMessage:
    body = (body or '').strip()
    if not body:
        raise ValueError('Message body is required.')

    ok, reason = session_is_usable(session)
    if not ok:
        raise ValueError(f'Live chat is not active: {reason}')

    msg = LiveChatMessage.objects.create(
        session=session,
        author_kind=author_kind,
        agent=agent if author_kind == 'agent' else None,
        body=body,
    )
    transaction.on_commit(lambda: broadcast_message(session, msg))
    return msg


def post_system_message(session: LiveChatSession, body: str) -> LiveChatMessage:
    return post_message(session, author_kind='system', body=body)


def absolute_customer_url(session: LiveChatSession, request=None) -> str:
    path = reverse('cs_live_chat_customer', kwargs={'token': str(session.token)})
    if request is not None:
        return request.build_absolute_uri(path)
    base = (getattr(settings, 'SITE_URL', '') or '').rstrip('/')
    return f'{base}{path}' if base else path


def send_invite_email(session: LiveChatSession, request=None) -> None:
    to = _safe_customer_email(session.ticket)
    if not to:
        logger.info('LiveChat invite skipped — ticket/customer has no email')
        return

    url = absolute_customer_url(session, request=request)
    subject = f'Live chat available — ticket {session.ticket.ticket_no}'
    body = (
        f'Hello {_safe_customer_name(session.ticket)},\n\n'
        f'Our Customer Service team has opened a live chat for your support ticket '
        f'{session.ticket.ticket_no}:\n\n'
        f'  "{session.ticket.subject}"\n\n'
        f'Please use the link below to start chatting with us. This link is tied '
        f'to the first device you open it on, so open it on the device you intend to use:\n\n'
        f'{url}\n\n'
        f'If you do not recognise this ticket, please disregard this email.\n\n'
        f'— Customer Service'
    )

    try:
        from django.core.mail import send_mail
        send_mail(
            subject,
            body,
            getattr(settings, 'DEFAULT_FROM_EMAIL', None) or 'no-reply@localhost',
            [to],
            fail_silently=False,
        )
        session.invitation_email_sent_at = timezone.now()
        session.save(update_fields=['invitation_email_sent_at', 'updated_at'])
    except Exception:
        logger.exception('LiveChat invite email failed for session %s', session.pk)


# Backward-compatible name for older uploaded views_extra.py code.
_send_invite_email = send_invite_email


def send_hijack_alert(session: LiveChatSession, attempt_ip: str, attempt_ua: str) -> None:
    recipients = set()
    for u in (getattr(session.ticket, 'current_owner', None), session.activated_by, getattr(session.ticket, 'created_by', None)):
        if u and getattr(u, 'email', ''):
            recipients.add(u.email)
    if not recipients:
        return

    subject = f'⚠️ Live chat lock — ticket {session.ticket.ticket_no}'
    body = (
        f'The live chat for ticket {session.ticket.ticket_no} ({session.ticket.subject}) has been LOCKED.\n\n'
        f'A second machine tried to open the tokenised chat link.\n\n'
        f'Original visitor: IP {session.first_visit_ip or "—"}\n'
        f'Original UA: {(session.first_visit_ua or "—")[:200]}\n\n'
        f'Hijack attempt: IP {attempt_ip or "—"}\n'
        f'Hijack UA: {(attempt_ua or "—")[:200]}\n\n'
        'If this was the legitimate customer switching devices, toggle the chat ON again from the ticket page.'
    )
    try:
        from django.core.mail import send_mail
        send_mail(
            subject,
            body,
            getattr(settings, 'DEFAULT_FROM_EMAIL', None) or 'no-reply@localhost',
            list(recipients),
            fail_silently=True,
        )
    except Exception:
        logger.warning('LiveChat hijack alert email failed for session %s', session.pk, exc_info=True)


def _get_channel_layer():
    try:
        from channels.layers import get_channel_layer
        return get_channel_layer()
    except Exception:
        return None


def broadcast_message(session: LiveChatSession, msg: LiveChatMessage) -> None:
    layer = _get_channel_layer()
    if not layer:
        return
    try:
        from asgiref.sync import async_to_sync
        async_to_sync(layer.group_send)(session.channels_group_name, {
            'type': 'chat.message',
            'message': {
                'id': msg.pk,
                'author_kind': msg.author_kind,
                'author_display': msg.author_display,
                'body': msg.body,
                'created_at': msg.created_at.isoformat(),
            },
        })
    except Exception:
        logger.warning('Channels broadcast failed for session %s', session.pk, exc_info=True)


def broadcast_session_state(session: LiveChatSession, *, kind: str) -> None:
    layer = _get_channel_layer()
    if not layer:
        return
    try:
        from asgiref.sync import async_to_sync
        async_to_sync(layer.group_send)(session.channels_group_name, {
            'type': 'chat.state',
            'state': {
                'kind': kind,
                'is_active': session.is_active,
                'is_locked': session.is_locked,
                'lock_reason': session.lock_reason,
            },
        })
    except Exception:
        logger.warning('Channels state broadcast failed for session %s', session.pk, exc_info=True)


# Backward-compatible names for older uploaded services.py references.
_broadcast_message = broadcast_message
_broadcast_session_state = broadcast_session_state
