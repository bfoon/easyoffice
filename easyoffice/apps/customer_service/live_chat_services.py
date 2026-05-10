"""
apps/customer_service/live_chat_services.py
────────────────────────────────────────────
Service layer for the tokenised live-chat between customers and CS agents.

Lives in its own module (rather than services.py) so the existing CS
services stay untouched. Imported by views_extra, signals, and the
Channels consumer.

Responsibilities:
    • Issue / rotate session tokens
    • Bind a session to the first machine that opens the link
    • Detect & handle hijack attempts (different machine, locked link)
    • Send invite / re-issued-link emails
    • Tear down sessions when tickets close
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


# ── Tunables ───────────────────────────────────────────────────────────

# Length of the machine_cookie value we issue. Long enough that brute-
# force guessing is hopeless even with no rate limiting.
MACHINE_COOKIE_BYTES = 24

# Name of the cookie we set on the customer's browser. Path-scoped to
# the live-chat URL so it doesn't leak elsewhere.
MACHINE_COOKIE_NAME = 'cs_livechat_mid'

# Sessions auto-expire after this if the ticket is still open. CS can
# always toggle ON again to issue a fresh link.
DEFAULT_EXPIRY_DAYS = 14


# ════════════════════════════════════════════════════════════════════════
# Session lifecycle
# ════════════════════════════════════════════════════════════════════════

def get_or_create_session(ticket: ServiceTicket) -> LiveChatSession:
    """Return the LiveChatSession for a ticket, creating one if needed."""
    session, _ = LiveChatSession.objects.get_or_create(ticket=ticket)
    return session


@transaction.atomic
def activate_session(ticket: ServiceTicket, *, actor, send_email: bool = True) -> LiveChatSession:
    """
    CS turns the chat ON.

    Always rotates the token (so any previously-shared link stops
    working) and re-emails the customer. Per project decision: every ON
    sends a fresh email — no silent re-activations.
    """
    session = get_or_create_session(ticket)
    session.regenerate_token()
    session.is_active = True
    session.activated_at = timezone.now()
    session.activated_by = actor if (actor and getattr(actor, 'is_authenticated', False)) else None
    session.expires_at = timezone.now() + timedelta(days=DEFAULT_EXPIRY_DAYS)
    session.deactivated_at = None
    session.save()

    ServiceTicketUpdate.objects.create(
        ticket=ticket,
        user=actor if (actor and getattr(actor, 'is_authenticated', False)) else None,
        update_type='note',
        body='💬 Live chat enabled. New chat link issued to the customer by email.',
    )

    if send_email:
        # Defer to commit so we don't email before the row is durable.
        transaction.on_commit(lambda: _send_invite_email(session))

    # Broadcast on the existing channels group so any open agent window
    # sees the chat refresh without a page reload. Best-effort.
    transaction.on_commit(lambda: _broadcast_session_state(session, kind='activated'))

    return session


@transaction.atomic
def deactivate_session(ticket: ServiceTicket, *, actor, reason: str = 'manually_off') -> LiveChatSession:
    """CS turns the chat OFF (or it's killed by ticket close, expiry, etc.)."""
    session = get_or_create_session(ticket)
    session.is_active = False
    session.deactivated_at = timezone.now()
    # If we're killing it for a reason other than "manually_off", remember why.
    if reason in {'ticket_closed', 'expired', 'different_machine'}:
        session.is_locked = True
        session.lock_reason = reason
        session.locked_at = timezone.now()
    session.save()

    if reason == 'manually_off':
        body = '💬 Live chat disabled by CS. Customer link no longer works.'
    elif reason == 'ticket_closed':
        body = '💬 Live chat closed because the ticket was closed.'
    elif reason == 'expired':
        body = '💬 Live chat link expired.'
    else:
        body = f'💬 Live chat deactivated ({reason}).'

    ServiceTicketUpdate.objects.create(
        ticket=ticket,
        user=actor if (actor and getattr(actor, 'is_authenticated', False)) else None,
        update_type='note',
        body=body,
    )

    transaction.on_commit(lambda: _broadcast_session_state(session, kind='deactivated'))
    return session


def session_is_usable(session: LiveChatSession) -> tuple[bool, str]:
    """
    Decide whether a customer hit on the chat URL should be allowed.

    Returns (ok, reason_code). reason_code is one of LiveChatSession.LOCK_REASONS
    keys when ok is False.
    """
    if not session.is_active:
        return False, 'manually_off'
    if session.is_locked:
        return False, session.lock_reason or 'different_machine'
    if session.expires_at and timezone.now() > session.expires_at:
        return False, 'expired'
    if session.ticket.status in {'closed', 'cancelled'}:
        return False, 'ticket_closed'
    return True, ''


# ════════════════════════════════════════════════════════════════════════
# Machine binding
# ════════════════════════════════════════════════════════════════════════

def new_machine_cookie_value() -> str:
    """Generate a random opaque cookie value for the customer's browser."""
    return secrets.token_urlsafe(MACHINE_COOKIE_BYTES)


def hash_fingerprint(raw: str) -> str:
    """
    Hash a fingerprint string so we don't store identifying browser
    metadata in plaintext. HMAC keyed on SECRET_KEY so the value is
    only meaningful within this deployment.
    """
    if not raw:
        return ''
    key = (getattr(settings, 'SECRET_KEY', '') or '').encode('utf-8')
    return hmac.new(
        key,
        raw.encode('utf-8', errors='ignore'),
        hashlib.sha256,
    ).hexdigest()


@transaction.atomic
def bind_first_visit(
    session: LiveChatSession,
    *,
    cookie_value: str,
    fingerprint_raw: str,
    ip: str,
    user_agent: str,
) -> tuple[bool, str]:
    """
    Called on every customer hit to the chat page. On the FIRST hit we
    record the machine binding; on subsequent hits we verify it.

    Returns (allowed, reason_code).
    """
    fp_hash = hash_fingerprint(fingerprint_raw)

    # First-ever visit — bind.
    if not session.machine_cookie:
        session.machine_cookie = cookie_value or new_machine_cookie_value()
        session.machine_fingerprint = fp_hash
        session.first_visit_at = timezone.now()
        session.first_visit_ip = ip or None
        session.first_visit_ua = (user_agent or '')[:400]
        session.save(update_fields=[
            'machine_cookie', 'machine_fingerprint',
            'first_visit_at', 'first_visit_ip', 'first_visit_ua',
            'updated_at',
        ])
        return True, ''

    # Subsequent visit — verify.
    cookie_matches = bool(cookie_value) and (cookie_value == session.machine_cookie)
    fp_matches = bool(fp_hash) and (fp_hash == session.machine_fingerprint)

    if cookie_matches:
        # Cookie is the primary key. Even if the fingerprint differs
        # (browser update, screen resize), we trust the cookie.
        return True, ''

    if fp_matches:
        # Cookie was cleared but the device is the same. Re-issue the
        # cookie under the existing value. (Caller will set the cookie
        # in the response.)
        return True, ''

    # ── Hijack attempt ─────────────────────────────────────────────
    lock_session(
        session,
        reason='different_machine',
        attempt_ip=ip,
        attempt_ua=user_agent,
    )
    return False, 'different_machine'


@transaction.atomic
def lock_session(
    session: LiveChatSession,
    *,
    reason: str,
    attempt_ip: str = '',
    attempt_ua: str = '',
) -> LiveChatSession:
    """Lock a session — typically called when a 2nd machine tries the link."""
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
            f'⚠️ Live chat LOCKED — a second machine tried to open the chat link.\n'
            f'Original visit: {session.first_visit_ip or "—"}\n'
            f'Hijack attempt: {attempt_ip or "—"}\n'
            f'UA: {(attempt_ua or "")[:200]}\n\n'
            f'The customer link no longer works. Toggle the chat ON again to '
            f'issue a fresh link.'
        ),
    )

    # Best-effort hijack alert to the assigned agent / current owner.
    transaction.on_commit(lambda: _send_hijack_alert(session, attempt_ip, attempt_ua))
    transaction.on_commit(lambda: _broadcast_session_state(session, kind='locked'))
    return session


# ════════════════════════════════════════════════════════════════════════
# Messaging
# ════════════════════════════════════════════════════════════════════════

@transaction.atomic
def post_message(
    session: LiveChatSession,
    *,
    author_kind: str,
    body: str,
    agent=None,
) -> LiveChatMessage:
    """Persist a message and broadcast it on the Channels group."""
    body = (body or '').strip()
    if not body:
        raise ValueError('Message body is required.')

    msg = LiveChatMessage.objects.create(
        session=session,
        author_kind=author_kind,
        agent=agent if author_kind == 'agent' else None,
        body=body,
    )

    transaction.on_commit(lambda: _broadcast_message(session, msg))
    return msg


def post_system_message(session: LiveChatSession, body: str) -> LiveChatMessage:
    """Convenience for system announcements ('CS turned chat off')."""
    return post_message(session, author_kind='system', body=body)


# ════════════════════════════════════════════════════════════════════════
# URL / email helpers
# ════════════════════════════════════════════════════════════════════════

def absolute_customer_url(session: LiveChatSession, request=None) -> str:
    """Build the full customer-facing chat URL with the current token."""
    path = reverse('cs_live_chat_customer', kwargs={'token': str(session.token)})
    if request is not None:
        return request.build_absolute_uri(path)
    base = (getattr(settings, 'SITE_URL', '') or '').rstrip('/')
    return f'{base}{path}' if base else path


def _send_invite_email(session: LiveChatSession) -> None:
    """Email the customer their fresh chat link. Best-effort."""
    contact = session.ticket.customer
    to = getattr(contact, 'email', '') or ''
    if not to:
        logger.info('LiveChat invite skipped — customer %s has no email', getattr(contact, 'pk', None))
        return

    url = absolute_customer_url(session)
    subject = f'Live chat available — ticket {session.ticket.ticket_no}'
    body = (
        f'Hello {contact.full_name or contact.display_name or "there"},\n\n'
        f'Our Customer Service team has opened a live chat for your '
        f'support ticket {session.ticket.ticket_no}:\n\n'
        f'  "{session.ticket.subject}"\n\n'
        f'Please use the link below to start chatting with us. The link '
        f'is tied to the first device you open it on, so open it on the '
        f'device you intend to use:\n\n'
        f'    {url}\n\n'
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


def _send_hijack_alert(session: LiveChatSession, attempt_ip: str, attempt_ua: str) -> None:
    """Email the assigned agent / ticket owner about a hijack attempt."""
    recipients = set()
    for u in (session.ticket.current_owner, session.activated_by, session.ticket.created_by):
        if u and getattr(u, 'email', ''):
            recipients.add(u.email)
    if not recipients:
        return

    subject = f'⚠️ Live chat lock — ticket {session.ticket.ticket_no}'
    body = (
        f'The live chat for ticket {session.ticket.ticket_no} ('
        f'{session.ticket.subject}) has been LOCKED.\n\n'
        f'A second machine tried to open the chat link issued to '
        f'{session.ticket.customer.display_name}.\n\n'
        f'  Original visitor:  IP {session.first_visit_ip or "—"}\n'
        f'                     UA {(session.first_visit_ua or "—")[:200]}\n'
        f'  Hijack attempt:    IP {attempt_ip or "—"}\n'
        f'                     UA {(attempt_ua or "—")[:200]}\n\n'
        f'The customer link has been deactivated. If this was the '
        f'legitimate customer switching devices, toggle the chat ON '
        f'again from the ticket page to issue a fresh link.'
    )
    try:
        from django.core.mail import send_mail
        send_mail(
            subject, body,
            getattr(settings, 'DEFAULT_FROM_EMAIL', None) or 'no-reply@localhost',
            list(recipients),
            fail_silently=True,
        )
    except Exception:
        logger.warning('LiveChat hijack alert email failed for session %s', session.pk, exc_info=True)


# ════════════════════════════════════════════════════════════════════════
# Channels broadcasting (lazy import so models load even if Channels isn't)
# ════════════════════════════════════════════════════════════════════════

def _get_channel_layer():
    try:
        from channels.layers import get_channel_layer
        return get_channel_layer()
    except Exception:
        return None


def _broadcast_message(session: LiveChatSession, msg: LiveChatMessage) -> None:
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
        logger.warning('Channels broadcast (message) failed for session %s', session.pk, exc_info=True)


def _broadcast_session_state(session: LiveChatSession, *, kind: str) -> None:
    """Tell anyone connected that the session state changed (on/off/locked)."""
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
        logger.warning('Channels broadcast (state) failed for session %s', session.pk, exc_info=True)
