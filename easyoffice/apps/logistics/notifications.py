"""
apps/logistics/notifications.py
===============================

Best-effort customer + internal notifications for shipment milestones.
Everything is wrapped by callers in try/except, so failures never block a
delivery action.
"""
from __future__ import annotations

import logging

from django.conf import settings

logger = logging.getLogger(__name__)


def _send_email(to_list, subject, body):
    if not to_list:
        return
    from django.core.mail import send_mail
    send_mail(
        subject, body,
        getattr(settings, 'DEFAULT_FROM_EMAIL', None),
        to_list, fail_silently=True,
    )


def notify_dispatched(shipment):
    """Tell the customer their delivery is on the way."""
    if not shipment.contact_email:
        return
    org = getattr(settings, 'OFFICE_NAME', 'Our Office')
    body = (
        f'Dear {shipment.contact_name or shipment.customer_name},\n\n'
        f'Your delivery ({shipment.reference}) has been dispatched from {shipment.origin_label} '
        f'and is on its way to:\n{shipment.destination_address}\n\n'
        f'Our driver will ask you to sign on arrival.\n\n{org}'
    )
    _send_email([shipment.contact_email], f'Your delivery is on the way — {shipment.reference}', body)


def notify_delivered(shipment):
    """Confirm delivery + sign-off to the customer."""
    if not shipment.contact_email:
        return
    org = getattr(settings, 'OFFICE_NAME', 'Our Office')
    pod = getattr(shipment, 'proof', None)
    signer = pod.received_by_name if pod else ''
    body = (
        f'Dear {shipment.contact_name or shipment.customer_name},\n\n'
        f'Your delivery ({shipment.reference}) has been completed'
        + (f' and signed for by {signer}' if signer else '')
        + f'.\n\nThank you for your business.\n{org}'
    )
    _send_email([shipment.contact_email], f'Delivery completed — {shipment.reference}', body)


def notify_internal_failed(shipment):
    """Alert logistics staff that a delivery failed."""
    try:
        from django.contrib.auth import get_user_model
        User = get_user_model()
        emails = list(
            User.objects.filter(
                is_active=True,
                groups__name__in=['Logistics', 'Admin', 'Office Manager'],
            ).values_list('email', flat=True)
        )
    except Exception:
        emails = []
    body = (
        f'Delivery {shipment.reference} to {shipment.customer_name} was marked FAILED.\n'
        f'Please review and reschedule.'
    )
    _send_email([e for e in emails if e], f'Delivery failed — {shipment.reference}', body)
