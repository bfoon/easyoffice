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
       the order's status and emails the buyer the signed PDF.

The handler is idempotent (each on_*_signed no-ops unless the order is in
the matching *_PENDING_SIGNATURE state), so it is safe to invoke both from
this signal AND directly from the one-click sign view.

⚠ IMPORTANT — why this file is structured the way it is:
The receiver MUST be a module-level function connected with weak=False.
The previous version defined the receiver as a function nested inside
connect_signature_signal(); Django signals hold weak references by
default, so once ready() returned, the nested function was garbage-
collected and the receiver silently disconnected. Result: signatures
completed but orders never advanced and buyers never received the
signed-PDF emails. Do not turn this back into a nested @receiver.

Wiring: call connect_signature_signal() from OrdersConfig.ready()
(see apps/orders/apps.py).
"""
from __future__ import annotations

import logging

from django.db.models.signals import post_save

logger = logging.getLogger(__name__)

_VALID_STAGES = {'proforma', 'invoice', 'delivery note'}


def _is_completed_status(value) -> bool:
    """SignatureRequestStatus.COMPLETED — handle either an enum or a string."""
    if value is None:
        return False
    s = getattr(value, 'value', value)
    return str(s).lower() in {'completed', 'signed', 'done', 'finished'}


def advance_order_after_signature(sig_req) -> bool:
    """
    Shared dispatcher: if `sig_req` is a completed, order-linked signature
    request, advance the order's state machine (which also emails the buyer
    the signed PDF). Returns True if a handler was invoked.

    Safe to call multiple times / from multiple places — the underlying
    services.on_*_signed handlers are idempotent.
    """
    if not _is_completed_status(getattr(sig_req, 'status', None)):
        return False

    meta = getattr(sig_req, 'metadata', None) or {}
    order_id = meta.get('orders.order_id')
    stage = (meta.get('orders.stage') or '').lower()

    if not order_id or stage not in _VALID_STAGES:
        return False

    from .models import SalesOrder
    from .permissions import can_sign_orders_documents
    from . import services

    try:
        order = SalesOrder.objects.get(pk=order_id)
    except SalesOrder.DoesNotExist:
        logger.error('Signature completed for unknown order %s', order_id)
        return False

    signer_row = (
        sig_req.signers
        .filter(status='signed', user__isnull=False)
        .select_related('user')
        .order_by('-signed_at', '-created_at')
        .first()
    )
    signer_user = signer_row.user if signer_row else None

    if not signer_user or not can_sign_orders_documents(signer_user):
        logger.warning(
            'Order %s signature completed without a valid CEO signer. '
            'Refusing to advance order state.',
            order.order_no,
        )
        return False

    logger.info(
        'Advancing order %s after %s signature (request %s).',
        order.order_no, stage, sig_req.pk,
    )
    try:
        if stage == 'proforma':
            services.on_proforma_signed(order, signature_request=sig_req, signer=signer_user)
        elif stage == 'invoice':
            services.on_invoice_signed(order, signature_request=sig_req, signer=signer_user)
        elif stage == 'delivery note':
            services.on_delivery_note_signed(order, signature_request=sig_req, signer=signer_user)
        return True
    except Exception:
        logger.exception(
            'Failed to advance order %s after signature completion',
            order.order_no,
        )
        return False


def _on_signature_request_saved(sender, instance, created, **kwargs):
    """Module-level post_save receiver — see module docstring for why."""
    if created:
        return
    advance_order_after_signature(instance)


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

    post_save.connect(
        _on_signature_request_saved,
        sender=SignatureRequest,
        weak=False,                              # ← the fix: survive GC
        dispatch_uid='orders.signature_completed',
    )
    logger.info('Orders ↔ signature bridge connected (weak=False).')