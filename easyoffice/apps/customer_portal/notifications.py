"""
apps/customer_portal/notifications.py
======================================

Thin wrapper around django.core.mail + the existing
apps.customer_service.notifications module. Two reasons to have our own
module instead of dumping calls into views:

  1. Centralises the "build a portal URL from a token" logic in one place,
     so we never accidentally email a relative link.
  2. Keeps the staff-facing CS notifications module untouched — it still
     does its job for tickets, while the portal module owns portal-specific
     email templates.

We piggy-back on the existing _absolute_url helper from
customer_service.notifications when we can, and fall back to a local
implementation if the import fails (e.g. during isolated tests).
"""
from __future__ import annotations

import logging
from typing import Iterable, Optional

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.urls import reverse, NoReverseMatch
from django.utils import timezone

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# URL building
# ─────────────────────────────────────────────────────────────────────────────

def _absolute_url(path: str) -> str:
    """
    Try the existing CS helper first (it has more fallback heuristics than
    we want to duplicate). Fall back to a tiny local version.
    """
    try:
        from apps.customer_service.notifications import _absolute_url as cs_abs
        return cs_abs(path)
    except Exception:
        pass

    # Local fallback — same shape but simpler
    if not path:
        path = '/'
    if not path.startswith('/'):
        path = '/' + path
    base = (getattr(settings, 'SITE_URL', '') or '').strip().rstrip('/')
    if not base:
        base = 'http://localhost:8000'
    return base + path


def _portal_url_for(token) -> str:
    try:
        path = reverse('customer_portal_home', kwargs={'token': token.token})
    except NoReverseMatch:
        path = f'/portal/{token.token}/'
    return _absolute_url(path)


def _from_email() -> str:
    return (
        getattr(settings, 'DEFAULT_FROM_EMAIL', None)
        or getattr(settings, 'SERVER_EMAIL', None)
        or 'no-reply@example.com'
    )


def _send_html_email(
    *,
    to: list[str],
    subject: str,
    text_body: str,
    html_body: str,
    cc: Optional[list[str]] = None,
) -> None:
    if not to:
        logger.warning('customer_portal: refusing to send email with no recipients (subject=%r)', subject)
        return

    msg = EmailMultiAlternatives(
        subject=subject,
        body=text_body,
        from_email=_from_email(),
        to=to,
        cc=cc or [],
    )
    msg.attach_alternative(html_body, 'text/html')
    try:
        msg.send(fail_silently=False)
    except Exception:
        logger.exception(
            'customer_portal: SMTP send failed for subject=%r to=%r', subject, to,
        )


# ─────────────────────────────────────────────────────────────────────────────
# Specific notifications
# ─────────────────────────────────────────────────────────────────────────────

def send_portal_invite(*, token, contract, contact, is_renewal: bool) -> None:
    """
    Sent on contract creation AND on renewal. The body is slightly
    different in each case so the customer knows why they're getting a
    new link.
    """
    portal_url = _portal_url_for(token)
    subject_prefix = 'Renewed contract' if is_renewal else 'Welcome'
    subject = f'{subject_prefix}: your support portal access for {contract.title}'

    ctx = {
        'token': token,
        'contract': contract,
        'contact': contact,
        'portal_url': portal_url,
        'is_renewal': is_renewal,
        'expires_on': token.valid_until,
    }

    text_body = render_to_string('customer_portal/email/portal_invite.txt', ctx)
    html_body = render_to_string('customer_portal/email/portal_invite.html', ctx)

    _send_html_email(
        to=[contact.email],
        subject=subject,
        text_body=text_body,
        html_body=html_body,
    )


def send_dispatch_notice(*, visit, contact_emails: Iterable[str]) -> None:
    """
    "A technician has been dispatched to your site." Sent when a
    MaintenanceVisit moves to status='dispatched'.
    """
    emails = [e for e in contact_emails if e]
    if not emails:
        return

    portal_url = _absolute_url('/portal/')  # fallback link
    contract = visit.contract

    # If we can find an active token for one of these emails, link directly.
    try:
        from .models import ContractContact, PortalAccessToken
        contact = (
            ContractContact.objects
            .filter(contract=contract, email__in=emails, is_active=True)
            .first()
        )
        if contact:
            tok = contact.active_token
            if tok:
                portal_url = _portal_url_for(tok)
    except Exception:
        logger.exception('customer_portal: could not resolve direct portal link for dispatch')

    ctx = {
        'visit': visit,
        'contract': contract,
        'portal_url': portal_url,
        'now': timezone.now(),
    }
    subject = f'Technician dispatched — {contract.title}'

    text_body = render_to_string('customer_portal/email/dispatch_notice.txt', ctx)
    html_body = render_to_string('customer_portal/email/dispatch_notice.html', ctx)

    _send_html_email(to=emails, subject=subject, text_body=text_body, html_body=html_body)


def send_on_site_notice(*, event, contact_emails: Iterable[str], internal_recipients=None) -> None:
    """
    Fan-out when a tech taps "I'm on site":

      * customer contacts get an email
      * internal recipients (CEO, Admin, Sales heads) get an in-app
        notification through the existing CS notifications layer
    """
    emails = [e for e in contact_emails if e]
    portal_url = _absolute_url('/portal/')

    try:
        from .models import ContractContact
        if event.contract_id and emails:
            contact = (
                ContractContact.objects
                .filter(contract_id=event.contract_id, email__in=emails, is_active=True)
                .first()
            )
            if contact:
                tok = contact.active_token
                if tok:
                    portal_url = _portal_url_for(tok)
    except Exception:
        logger.exception('customer_portal: could not resolve direct portal link for on-site')

    if emails:
        ctx = {
            'event': event,
            'task': event.task,
            'portal_url': portal_url,
            'now': timezone.now(),
        }
        subject = f'Technician arrived on site — {getattr(event.task, "title", "your service request")}'
        text_body = render_to_string('customer_portal/email/on_site_notice.txt', ctx)
        html_body = render_to_string('customer_portal/email/on_site_notice.html', ctx)
        _send_html_email(to=emails, subject=subject, text_body=text_body, html_body=html_body)

    # In-app for staff (CEO, Admin, Sales heads).
    if internal_recipients:
        try:
            from apps.customer_service.notifications import _send_inapp
            for u in internal_recipients:
                _send_inapp(
                    u,
                    title='Technician on site',
                    body=(
                        f'{event.actor_user.full_name if event.actor_user else "A technician"} '
                        f'arrived on site for task "{event.task.title}".'
                    ),
                    url=f'/tasks/{event.task_id}/',
                    kind='customer_portal',
                )
        except Exception:
            logger.exception('customer_portal: in-app on-site fan-out failed')


def send_dispute_notice(*, event, internal_recipients) -> None:
    """
    Customer says "the tech said they were on site but never showed up."
    Goes IN-APP only to: CS owner of the ticket, CEO, Admin, Sales head.
    Email is overkill for this — it goes to the people who are already on
    the dashboard.
    """
    try:
        from apps.customer_service.notifications import _send_inapp
        for u in internal_recipients:
            _send_inapp(
                u,
                title='⚠️ Customer disputes on-site visit',
                body=(
                    f'A customer reports that the technician marked themselves '
                    f'on site but did not show up. Task: "{event.task.title}".'
                ),
                url=f'/tasks/{event.task_id}/',
                kind='customer_portal_dispute',
            )
    except Exception:
        logger.exception('customer_portal: dispute fan-out failed')


def send_task_done_pending_verify(*, task, cs_owner, contact_emails: Iterable[str]) -> None:
    """
    The technician marked the task done. Don't close the ticket yet — tell
    the CS owner they need to verify with the customer first.
    """
    try:
        from apps.customer_service.notifications import _send_inapp
        if cs_owner:
            _send_inapp(
                cs_owner,
                title='Awaiting your verification',
                body=(
                    f'Technician {task.assigned_to.full_name if task.assigned_to else "(unknown)"} '
                    f'marked "{task.title}" as done. Please confirm with the customer '
                    f'before closing the ticket.'
                ),
                url=f'/tasks/{task.id}/',
                kind='customer_portal_verify',
            )
    except Exception:
        logger.exception('customer_portal: cs-verify fan-out failed')

    # Optional courtesy email to the customer — "we're verifying your visit".
    emails = [e for e in (contact_emails or []) if e]
    if emails:
        ctx = {'task': task, 'now': timezone.now()}
        subject = f'Verifying completion of: {task.title}'
        try:
            text_body = render_to_string('customer_portal/email/verify_pending.txt', ctx)
            html_body = render_to_string('customer_portal/email/verify_pending.html', ctx)
            _send_html_email(to=emails, subject=subject, text_body=text_body, html_body=html_body)
        except Exception:
            # Templates missing? Don't crash — log.
            logger.exception('customer_portal: verify-pending email failed')
