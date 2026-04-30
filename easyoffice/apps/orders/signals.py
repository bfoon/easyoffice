"""
apps/orders/signals.py
──────────────────────
Bridge between the files-app SignatureRequest and the orders state machine.

When the CEO completes a signature on a Proforma / Invoice / Delivery Note
that originated from an order, we:

    1. Look up the SalesOrder via the metadata we stored when creating the
       request (orders.order_id + orders.stage).
    2. Verify the signer is actually the CEO (defence in depth — if for any
       reason the signature ends up on a non-CEO account, refuse to advance).
    3. Call the corresponding services.on_*_signed() handler, which flips
       the order's status and emails the buyer.

The handler is idempotent. We trigger on a status transition to "completed",
not on every save, by checking the previous status before the save.

Wiring: register this in apps/orders/apps.py → OrdersConfig.ready().
"""
from __future__ import annotations

import logging

from django.db.models.signals import post_save
from django.dispatch import receiver

logger = logging.getLogger(__name__)


# We pull the SignatureRequest model lazily inside the handler to avoid app-
# loading order issues, and connect via the string sender below.

def _is_completed_status(value) -> bool:
    """SignatureRequestStatus.COMPLETED — handle either an enum or a string."""
    if value is None:
        return False
    s = getattr(value, 'value', value)
    return str(s).lower() in {'completed', 'signed', 'done', 'finished'}


def connect_signature_signal():
    """
    Called from OrdersConfig.ready(). Hooked at runtime so we can fail-soft
    if the files app isn't installed yet (e.g. during initial migrate).
    """
    try:
        from apps.files.models import SignatureRequest
    except Exception:  # pragma: no cover
        logger.warning(
            'apps.files.SignatureRequest not importable — '
            'orders signature gating disabled.'
        )
        return

    @receiver(post_save, sender=SignatureRequest, dispatch_uid='orders.signature_completed')
    def _on_signature_request_saved(sender, instance, created, **kwargs):
        if created:
            return

        if not _is_completed_status(getattr(instance, 'status', None)):
            return

        meta = getattr(instance, 'metadata', None) or {}
        order_id = meta.get('orders.order_id')
        stage = (meta.get('orders.stage') or '').lower()

        if not order_id or stage not in {'proforma', 'invoice', 'delivery note'}:
            return

        from .models import SalesOrder
        from .permissions import can_sign_orders_documents
        from . import services

        try:
            order = SalesOrder.objects.get(pk=order_id)
        except SalesOrder.DoesNotExist:
            logger.error('Signature completed for unknown order %s', order_id)
            return

        signer_row = (
            instance.signers
            .filter(status='signed', user__isnull=False)
            .select_related('user')
            .order_by('-signed_at', '-created_at')
            .first()
        )
        signer_user = signer_row.user if signer_row else None

        if not signer_user or not can_sign_orders_documents(signer_user):
            logger.warning(
                'Order %s signature completed without a valid CEO signer. Refusing to advance order state.',
                order.order_no,
            )
            return

        try:
            if stage == 'proforma':
                services.on_proforma_signed(order, signature_request=instance, signer=signer_user)
            elif stage == 'invoice':
                services.on_invoice_signed(order, signature_request=instance, signer=signer_user)
            elif stage == 'delivery note':
                services.on_delivery_note_signed(order, signature_request=instance, signer=signer_user)
        except Exception:
            logger.exception(
                'Failed to advance order %s after signature completion',
                order.order_no,
            )