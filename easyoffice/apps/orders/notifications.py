"""
apps/orders/notifications.py
────────────────────────────
Single source of truth for order-related notifications.

Two channels per event:
    • in-app : platform Notification rows (model auto-discovered at runtime)
    • email  : django.core.mail.EmailMessage with rendered HTML body

Each public function is named `notify_<event>` and takes the order plus
whatever extras it needs. They never raise — failure to notify must not
break the underlying business action.

Call these from apps/orders/services.py *after* the DB transaction commits
(via `transaction.on_commit(lambda: notify_xxx(order))`).

Notification matrix
═══════════════════
Event                              In-app recipients              Email recipients
─────────────────────────────────────────────────────────────────────────────────
created (intake)                   CS group queue                 — (no email yet)
created (website/API)              CS + supervisors               Customer (ack)
needs_attention                    CS group queue                 —
confirmed                          creator + Sales group          Customer (summary)

proforma_pending_signature         CEO + creator                  CEO (sign request link)
proforma_ready (= signed)          fulfilled_by + creator         Customer (signed PDF)
invoice_pending_signature          CEO + creator                  CEO (sign request link)
invoice_issued (= signed)          fulfilled_by + Finance         Customer (signed PDF)
delivery_note_pending_signature    CEO + creator                  CEO (sign request link)
delivery_note_issued (= signed)    fulfilled_by + Logistics       Customer (signed PDF)

cancelled                          creator + Sales supervisors    Customer (with reason)
customer_attached                  creator                        —
"""
from __future__ import annotations

import logging
from typing import Iterable, Optional

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.mail import EmailMessage
from django.template.loader import render_to_string
from django.urls import reverse

logger = logging.getLogger(__name__)
User = get_user_model()


# ── Group names (lower-case match against Group.name) ──────────────────────
GROUP_CS         = ('customer service', 'customer_service', 'cs')
GROUP_SALES      = ('sales',)
GROUP_FINANCE    = ('finance', 'accounts')
GROUP_LOGISTICS  = ('logistics', 'warehouse', 'dispatch')
GROUP_SUPERVISOR = ('supervisor', 'manager', 'admin', 'administrator', 'head of sales')
GROUP_CEO        = ('ceo',)


# ════════════════════════════════════════════════════════════════════════
# Low-level helpers
# ════════════════════════════════════════════════════════════════════════

def _users_in_groups(*group_name_sets) -> "Iterable[User]":
    names = {n.lower() for s in group_name_sets for n in s}
    if not names:
        return User.objects.none()
    return (
        User.objects
        .filter(is_active=True, groups__name__iregex=r'^(' + '|'.join(names) + r')$')
        .distinct()
    )


def _site_url() -> str:
    """
    Return the configured SITE_URL with a clear, single-warning fallback.

    Outbound emails need an absolute URL because the recipient's mail
    client has no notion of "the same host as the sender." If SITE_URL
    isn't set, links arrive as bare paths like `/files/signatures/<uuid>/`
    which are useless. We warn loudly once so it gets fixed.
    """
    site_url = getattr(settings, 'SITE_URL', '') or ''
    if not site_url:
        if not getattr(_site_url, '_warned', False):
            logger.warning(
                'settings.SITE_URL is not configured — outbound email links '
                'will use a relative path. Set SITE_URL in settings.py or as '
                'an environment variable to your full domain (e.g. '
                'https://easyoffice.example.org).'
            )
            _site_url._warned = True
        return ''
    return site_url.rstrip('/')


def _build_order_url(order) -> str:
    path = reverse('orders:order_detail', kwargs={'pk': order.pk})
    return f'{_site_url()}{path}'


def _build_signature_url(signature_request) -> str:
    """Absolute URL to the signature request detail page (for the CEO)."""
    try:
        path = reverse('signature_request_detail', kwargs={'pk': signature_request.pk})
    except Exception:
        return ''
    return f'{_site_url()}{path}'


# ── In-app notification model discovery ────────────────────────────────────

_NOTIFICATION_MODEL = None
_NOTIFICATION_FIELDS = None


def _discover_notification_model():
    """
    Find the project's in-app notification model. Cached after first call.
    Returns (Model, field_map) or (None, None) if nothing's found.

    field_map keys (model field name) → role:
        'recipient'  → who receives it (User FK)
        'kind'       → category/type string
        'title'      → headline string
        'body'       → details string
        'link'       → URL string
        'payload'    → JSON dict for extras
    """
    global _NOTIFICATION_MODEL, _NOTIFICATION_FIELDS
    if _NOTIFICATION_MODEL is not None or _NOTIFICATION_FIELDS == {}:
        return _NOTIFICATION_MODEL, _NOTIFICATION_FIELDS

    from django.apps import apps as django_apps

    candidates = []
    for model in django_apps.get_models():
        name_lower = model.__name__.lower()
        if 'notification' not in name_lower:
            continue
        if any(skip in name_lower for skip in ('preference', 'setting', 'config', 'subscription')):
            continue
        candidates.append(model)

    if not candidates:
        logger.warning('No Notification model discovered in any installed app.')
        _NOTIFICATION_FIELDS = {}
        return None, None

    chosen = next(
        (m for m in candidates if m._meta.app_label == 'notifications'),
        candidates[0],
    )

    field_names = {f.name for f in chosen._meta.get_fields()}
    fk_to_user = [
        f.name for f in chosen._meta.get_fields()
        if f.is_relation and getattr(f, 'many_to_one', False)
           and getattr(f.related_model, '__name__', '') == User.__name__
    ]

    field_map = {
        'recipient': next(
            (n for n in ('recipient', 'user', 'to_user', 'target_user', 'addressee') if n in field_names),
            (fk_to_user[0] if fk_to_user else None),
        ),
        'kind': next((n for n in ('kind', 'category', 'type', 'notification_type', 'event') if n in field_names), None),
        'title': next((n for n in ('title', 'subject', 'headline', 'name') if n in field_names), None),
        'body': next((n for n in ('body', 'message', 'description', 'content', 'text') if n in field_names), None),
        'link': next((n for n in ('link', 'url', 'action_url', 'target_url', 'href') if n in field_names), None),
        'payload': next(
            (n for n in ('payload', 'data', 'meta', 'metadata', 'extra', 'extra_data', 'context') if n in field_names),
            None),
    }

    if not field_map['recipient']:
        logger.warning(
            'Notification model %s has no recognisable recipient FK — in-app dispatch disabled.',
            chosen.__name__,
        )
        _NOTIFICATION_FIELDS = {}
        return None, None

    logger.info(
        'Notification model discovered: %s.%s with field map %s',
        chosen._meta.app_label, chosen.__name__, field_map,
    )
    _NOTIFICATION_MODEL = chosen
    _NOTIFICATION_FIELDS = field_map
    return chosen, field_map


def _create_in_app(recipients, *, kind: str, title: str, body: str, order, link=None):
    """Write Notification rows. Auto-adapts to the project's notification model."""
    if not recipients:
        return

    Notification, fields = _discover_notification_model()
    if Notification is None:
        logger.debug('No notification model wired — skipping in-app dispatch for %s', kind)
        return

    link = link or reverse('orders:order_detail', kwargs={'pk': order.pk})

    rows = []
    for u in recipients:
        kwargs = {fields['recipient']: u}
        if fields['kind']:
            kwargs[fields['kind']] = kind
        if fields['title']:
            kwargs[fields['title']] = title[:200]
        if fields['body']:
            kwargs[fields['body']] = body[:500]
        if fields['link']:
            kwargs[fields['link']] = link[:500]
        if fields['payload']:
            kwargs[fields['payload']] = {
                'order_id': str(order.pk),
                'order_no': order.order_no,
                'status': order.status,
            }
        rows.append(Notification(**kwargs))

    try:
        Notification.objects.bulk_create(rows)
        logger.info('Created %d in-app notifications (kind=%s)', len(rows), kind)
    except Exception:
        logger.exception('Failed to create in-app notifications for %s', order.order_no)


def _send_email(*, to: list, subject: str, template: str, context: dict,
                attachments: Optional[list] = None, reply_to: Optional[list] = None):
    to = [a for a in (to or []) if a]
    if not to:
        return
    try:
        body = render_to_string(template, context)
        msg = EmailMessage(
            subject=subject,
            body=body,
            from_email=getattr(settings, 'DEFAULT_FROM_EMAIL', None),
            to=to,
            reply_to=reply_to or [getattr(settings, 'ORDERS_REPLY_TO', settings.DEFAULT_FROM_EMAIL)],
        )
        msg.content_subtype = 'html'
        for att in (attachments or []):
            msg.attach(*att)
        msg.send(fail_silently=False)
    except Exception:
        logger.exception('Failed to send order email "%s" to %s', subject, to)


def _customer_email(order) -> Optional[str]:
    if order.customer_id and getattr(order.customer, 'email', None):
        return order.customer.email
    return order.contact_email or None


# ════════════════════════════════════════════════════════════════════════
# Order creation / confirmation
# ════════════════════════════════════════════════════════════════════════

def notify_order_created(order, *, actor=None):
    cs_users = _users_in_groups(GROUP_CS)
    _create_in_app(
        cs_users,
        kind='orders.created',
        title=f'New order {order.order_no}',
        body=f'{order.get_source_display()} · {order.currency} {order.total}',
        order=order,
    )
    if order.source in ('website', 'api', 'email'):
        _create_in_app(
            _users_in_groups(GROUP_SUPERVISOR),
            kind='orders.created.external',
            title=f'External order {order.order_no} received',
            body=f'Source: {order.get_source_display()}. Needs review.',
            order=order,
        )
        _send_email(
            to=[_customer_email(order)],
            subject=f'We received your order {order.order_no}',
            template='orders/email/order_received.html',
            context={'order': order, 'order_url': _build_order_url(order)},
        )


def notify_order_needs_attention(order, *, reason: str = ''):
    _create_in_app(
        _users_in_groups(GROUP_CS),
        kind='orders.unattached',
        title=f'Order {order.order_no} needs attention',
        body=reason or 'Customer not linked or contact info incomplete.',
        order=order,
    )


def notify_order_confirmed(order, *, actor=None):
    """Customer gets the confirmation email — this is the moment they know it's locked in."""
    recipients = []
    if order.created_by_id:
        recipients.append(order.created_by)
    recipients += list(_users_in_groups(GROUP_SALES))
    _create_in_app(
        recipients,
        kind='orders.confirmed',
        title=f'Order {order.order_no} confirmed',
        body=f'{order.currency} {order.total} · {order.items.count()} items',
        order=order,
    )
    _send_email(
        to=[_customer_email(order)],
        subject=f'Order {order.order_no} confirmed',
        template='orders/email/order_confirmed.html',
        context={'order': order, 'order_url': _build_order_url(order)},
    )


# ════════════════════════════════════════════════════════════════════════
# Pending-signature notifications (CEO inbox)
# ════════════════════════════════════════════════════════════════════════

def _notify_doc_pending_signature(order, *, doc_label: str, kind: str, signature_request, actor=None):
    """Shared body for proforma/invoice/DN pending-signature events."""
    ceos = list(_users_in_groups(GROUP_CEO))
    recipients = list(ceos)
    if order.created_by_id and order.created_by not in recipients:
        recipients.append(order.created_by)
    if actor and actor not in recipients:
        recipients.append(actor)

    # In-app link goes to the order detail (CTA there takes them to signing) —
    # better than dropping straight into the files-app signing UI because the
    # CEO sees context about what they're signing.
    _create_in_app(
        recipients,
        kind=kind,
        title=f'{doc_label} awaiting your signature — {order.order_no}',
        body=(
            f'{doc_label} for {order.contact_name or "customer"} is ready '
            f'for review and signature.'
        ),
        order=order,
    )

    # Email the CEO with a direct link to sign
    ceo_emails = [u.email for u in ceos if u.email]
    sig_url = _build_signature_url(signature_request) if signature_request else ''
    _send_email(
        to=ceo_emails,
        subject=f'[Sign] {doc_label} {getattr(signature_request, "pk", "")} — Order {order.order_no}',
        template='orders/email/ceo_signature_request.html',
        context={
            'order': order,
            'doc_label': doc_label,
            'signature_url': sig_url,
            'order_url': _build_order_url(order),
        },
    )


def notify_proforma_pending_signature(order, *, actor=None, signature_request=None):
    _notify_doc_pending_signature(
        order, doc_label='Proforma',
        kind='orders.proforma_pending_signature',
        signature_request=signature_request, actor=actor,
    )


def notify_invoice_pending_signature(order, *, actor=None, signature_request=None):
    _notify_doc_pending_signature(
        order, doc_label='Invoice',
        kind='orders.invoice_pending_signature',
        signature_request=signature_request, actor=actor,
    )


def notify_delivery_note_pending_signature(order, *, actor=None, signature_request=None):
    _notify_doc_pending_signature(
        order, doc_label='Delivery Note',
        kind='orders.delivery_note_pending_signature',
        signature_request=signature_request, actor=actor,
    )


# ════════════════════════════════════════════════════════════════════════
# Post-signature (= "ready") notifications — emailed to buyer
# ════════════════════════════════════════════════════════════════════════

def notify_proforma_ready(order, *, actor=None, pdf_bytes: Optional[bytes] = None):
    """Proforma signed by CEO. Email customer the signed PDF; ping the agent."""
    recipients = []
    if order.fulfilled_by_id:
        recipients.append(order.fulfilled_by)
    if order.created_by_id and order.created_by not in recipients:
        recipients.append(order.created_by)
    _create_in_app(
        recipients,
        kind='orders.proforma_ready',
        title=f'Proforma signed for {order.order_no}',
        body=f'{order.proforma.number if order.proforma_id else ""} — Invoice step now unlocked.',
        order=order,
    )

    attachments = []
    if pdf_bytes and order.proforma_id:
        attachments.append((f'{order.proforma.number}.pdf', pdf_bytes, 'application/pdf'))
    _send_email(
        to=[_customer_email(order)],
        subject=f'Proforma {order.proforma.number if order.proforma_id else order.order_no}',
        template='orders/email/proforma_ready.html',
        context={'order': order, 'order_url': _build_order_url(order)},
        attachments=attachments,
    )


def notify_invoice_issued(order, *, actor=None, pdf_bytes: Optional[bytes] = None):
    recipients = []
    if order.fulfilled_by_id:
        recipients.append(order.fulfilled_by)
    if actor and actor not in recipients:
        recipients.append(actor)
    recipients += list(_users_in_groups(GROUP_FINANCE))
    _create_in_app(
        recipients,
        kind='orders.invoice_issued',
        title=f'Invoice signed — {order.order_no}',
        body=f'{order.invoice.number if order.invoice_id else ""} · {order.currency} {order.total}',
        order=order,
    )

    attachments = []
    if pdf_bytes and order.invoice_id:
        attachments.append((f'{order.invoice.number}.pdf', pdf_bytes, 'application/pdf'))
    _send_email(
        to=[_customer_email(order)],
        subject=f'Invoice {order.invoice.number if order.invoice_id else order.order_no}',
        template='orders/email/invoice_issued.html',
        context={'order': order, 'order_url': _build_order_url(order)},
        attachments=attachments,
    )


def notify_delivery_note_issued(order, *, actor=None, pdf_bytes: Optional[bytes] = None):
    recipients = []
    if order.fulfilled_by_id:
        recipients.append(order.fulfilled_by)
    if actor and actor not in recipients:
        recipients.append(actor)
    logistics_users = list(_users_in_groups(GROUP_LOGISTICS))
    recipients += logistics_users
    _create_in_app(
        recipients,
        kind='orders.delivery_note',
        title=f'Order {order.order_no} fulfilled',
        body=f'Delivery note {order.delivery_note.number if order.delivery_note_id else ""} signed and issued.',
        order=order,
    )

    attachments = []
    if pdf_bytes and order.delivery_note_id:
        attachments.append((f'{order.delivery_note.number}.pdf', pdf_bytes, 'application/pdf'))

    # Email the customer — "your order is on its way"
    _send_email(
        to=[_customer_email(order)],
        subject=f'Your order {order.order_no} is on its way',
        template='orders/email/delivery_note_issued.html',
        context={'order': order, 'order_url': _build_order_url(order)},
        attachments=attachments,
    )

    # Email logistics — "prep this for dispatch", with PDF attached for the
    # picker. Different template + subject because the audience is different.
    logistics_emails = [u.email for u in logistics_users if getattr(u, 'email', None)]
    if logistics_emails:
        _send_email(
            to=logistics_emails,
            subject=f'[Dispatch] Order {order.order_no} ready for delivery',
            template='orders/email/delivery_note_logistics.html',
            context={'order': order, 'order_url': _build_order_url(order)},
            attachments=attachments,
        )


# ════════════════════════════════════════════════════════════════════════
# Cancellation / customer link
# ════════════════════════════════════════════════════════════════════════

def notify_order_cancelled(order, *, actor=None, reason: str = ''):
    recipients = []
    if order.created_by_id:
        recipients.append(order.created_by)
    recipients += list(_users_in_groups(GROUP_SALES, GROUP_SUPERVISOR))
    _create_in_app(
        recipients,
        kind='orders.cancelled',
        title=f'Order {order.order_no} cancelled',
        body=reason or 'No reason provided.',
        order=order,
    )
    _send_email(
        to=[_customer_email(order)],
        subject=f'Order {order.order_no} cancelled',
        template='orders/email/order_cancelled.html',
        context={'order': order, 'reason': reason, 'order_url': _build_order_url(order)},
    )


def notify_customer_attached(order, *, actor=None):
    if not order.created_by_id:
        return
    _create_in_app(
        [order.created_by],
        kind='orders.customer_attached',
        title=f'Customer linked to {order.order_no}',
        body=f'{getattr(order.customer, "display_name", None) or getattr(order.customer, "full_name", "Customer")} attached.',
        order=order,
    )