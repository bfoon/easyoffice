"""
apps/inventory/notifications.py
───────────────────────────────
Inventory event notifications. Same shape as apps/orders/notifications.py
— two channels (in-app + email), called from services.py on transaction
commit, and never raise on failure.

Event matrix
════════════
Event                       In-app recipients          Email recipients
─────────────────────────────────────────────────────────────────────────
stock_received              Inventory team             —
stock_transferred           Inventory team             —
low_stock                   Inventory + supervisors    Inventory supervisor
asset_assigned              Assignee + manager         Assignee
asset_scrapped              Inventory + CEO            CEO
stock_request_submitted     Inventory team             —
stock_request_issued        Requester                  Requester
stock_request_rerouted      Requester + Finance        Requester + Finance
stock_take_completed        Inventory + supervisors    —
"""
from __future__ import annotations

import logging
from typing import Iterable, Optional

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.mail import EmailMessage
from django.urls import reverse

logger = logging.getLogger(__name__)
User = get_user_model()


# ── Group buckets (lower-case match) ────────────────────────────────────────
GROUP_INVENTORY  = ('inventory', 'stock', 'storekeeper', 'warehouse', 'logistics')
GROUP_SUPERVISOR = ('supervisor', 'manager', 'admin', 'administrator', 'head of operations')
GROUP_FINANCE    = ('finance', 'accounts')
GROUP_CEO        = ('ceo',)


# ════════════════════════════════════════════════════════════════════════════
# Helpers
# ════════════════════════════════════════════════════════════════════════════

def _users_in_groups(*group_name_sets) -> Iterable:
    names = {n.lower() for s in group_name_sets for n in s}
    if not names:
        return User.objects.none()
    return (
        User.objects
        .filter(is_active=True, groups__name__iregex=r'^(' + '|'.join(names) + r')$')
        .distinct()
    )


def _site_url() -> str:
    url = getattr(settings, 'SITE_URL', '') or ''
    if not url:
        logger.warning('SITE_URL not set — using relative paths in email.')
    return url.rstrip('/')


def _create_inapp(recipient, *, title: str, body: str, url: str = '', kind: str = 'inventory'):
    """
    Best-effort write to the platform's notification model. We discover
    the model lazily — if the project doesn't have one, we just skip.
    """
    if not recipient:
        return
    try:
        # Most common in this project
        from apps.core.models import Notification  # type: ignore
    except Exception:
        try:
            from apps.notifications.models import Notification  # type: ignore
        except Exception:
            Notification = None  # type: ignore

    if Notification is None:
        return

    try:
        Notification.objects.create(
            recipient=recipient,
            title=title[:200],
            message=body[:500],
            url=url[:300] if url else '',
        )
    except TypeError:
        # Different field names — try a more conservative payload
        try:
            Notification.objects.create(user=recipient, title=title[:200], message=body[:500])
        except Exception:
            logger.exception('Could not write inventory in-app notification.')
    except Exception:
        logger.exception('Could not write inventory in-app notification.')


def _send_email(recipients: list[str], subject: str, body: str):
    recipients = [r for r in recipients if r]
    if not recipients:
        return
    try:
        EmailMessage(
            subject=subject,
            body=body,
            from_email=getattr(settings, 'DEFAULT_FROM_EMAIL', None),
            to=recipients,
        ).send(fail_silently=True)
    except Exception:
        logger.exception('Inventory email failed: %s', subject)


# ════════════════════════════════════════════════════════════════════════════
# Stock-movement notifications
# ════════════════════════════════════════════════════════════════════════════

def notify_stock_received(product, location, quantity, *, actor=None):
    title = f'Stock received: {product.name} ×{quantity}'
    body  = f'{quantity} {product.unit_label}(s) of {product.sku} received at {location.code}.'
    try:
        url = reverse('inventory:product_detail', args=[product.pk])
    except Exception:
        url = ''
    for u in _users_in_groups(GROUP_INVENTORY):
        _create_inapp(u, title=title, body=body, url=url)


def notify_stock_transferred(product, from_loc, to_loc, quantity, *, actor=None):
    title = f'Transfer: {product.name}'
    body  = f'{quantity} of {product.sku} moved from {from_loc.code} to {to_loc.code}.'
    try:
        url = reverse('inventory:product_detail', args=[product.pk])
    except Exception:
        url = ''
    for u in _users_in_groups(GROUP_INVENTORY):
        _create_inapp(u, title=title, body=body, url=url)


def notify_low_stock(product, *, actor=None):
    title = f'⚠ Low stock: {product.name}'
    body  = (
        f'{product.sku} is at or below its reorder point '
        f'({product.total_available} available; reorder at {product.reorder_point}).'
    )
    try:
        url = reverse('inventory:product_detail', args=[product.pk])
    except Exception:
        url = ''

    for u in _users_in_groups(GROUP_INVENTORY, GROUP_SUPERVISOR):
        _create_inapp(u, title=title, body=body, url=url)

    # Email the supervisors only — they're the ones who escalate to a PO
    emails = [u.email for u in _users_in_groups(GROUP_SUPERVISOR) if u.email]
    if emails:
        site = _site_url()
        full_url = f'{site}{url}' if site and url else url
        _send_email(
            emails,
            subject=f'[EasyOffice] Low stock alert — {product.sku}',
            body=(
                f'{title}\n\n{body}\n\n'
                f'Suggested re-order quantity: {product.reorder_qty}.\n'
                f'View product: {full_url}'
            ),
        )


# ════════════════════════════════════════════════════════════════════════════
# Asset notifications
# ════════════════════════════════════════════════════════════════════════════

def notify_asset_assigned(asset, assignee, *, actor=None):
    title = f'Asset assigned: {asset.tag}'
    body  = f'{asset.name} has been assigned to {assignee}.'
    try:
        url = reverse('inventory:asset_detail', args=[asset.pk])
    except Exception:
        url = ''
    _create_inapp(assignee, title=title, body=body, url=url)
    for u in _users_in_groups(GROUP_INVENTORY):
        _create_inapp(u, title=title, body=body, url=url)
    if getattr(assignee, 'email', None):
        site = _site_url()
        full_url = f'{site}{url}' if site and url else url
        _send_email(
            [assignee.email],
            subject=f'[EasyOffice] Asset assigned to you: {asset.tag}',
            body=(
                f'{asset.name} (tag {asset.tag}) has been assigned to you.\n\n'
                f'You are responsible for its safe-keeping until returned.\n'
                f'View asset: {full_url}'
            ),
        )


def notify_asset_scrapped(asset, *, actor=None, reason: str = ''):
    title = f'Asset disposed: {asset.tag}'
    body  = reason or f'{asset.name} marked as disposed.'
    for u in _users_in_groups(GROUP_INVENTORY, GROUP_CEO):
        _create_inapp(u, title=title, body=body)


# ════════════════════════════════════════════════════════════════════════════
# Stock-request notifications
# ════════════════════════════════════════════════════════════════════════════

def notify_stock_request_submitted(sr):
    title = f'Stock request {sr.reference}'
    body  = f'New stock request from {sr.requested_by} — {sr.lines.count()} line(s).'
    try:
        url = reverse('inventory:stock_request_detail', args=[sr.pk])
    except Exception:
        url = ''
    for u in _users_in_groups(GROUP_INVENTORY):
        _create_inapp(u, title=title, body=body, url=url)


def notify_stock_request_issued(sr, *, actor=None):
    title = f'Stock request issued: {sr.reference}'
    body  = f'{sr.get_status_display()}. Issued by {actor or sr.issued_by}.'
    _create_inapp(sr.requested_by, title=title, body=body)


def notify_stock_request_rerouted(sr, purchase_request, *, actor=None):
    title = f'Out of stock — purchase requested ({sr.reference})'
    body  = (
        f'Some items on stock request {sr.reference} were not in stock '
        f'and have been rerouted to a new purchase request.'
    )
    _create_inapp(sr.requested_by, title=title, body=body)
    for u in _users_in_groups(GROUP_FINANCE):
        _create_inapp(u, title=title, body=body)


def notify_stock_take_completed(st, *, actor=None):
    title = f'Stock-take {st.reference} completed'
    body  = f'{st.lines.count()} lines reconciled at {st.location.code}.'
    for u in _users_in_groups(GROUP_INVENTORY, GROUP_SUPERVISOR):
        _create_inapp(u, title=title, body=body)
