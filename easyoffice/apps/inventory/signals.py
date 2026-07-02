"""
apps/inventory/signals.py
─────────────────────────
Bridges from other apps into the inventory ledger.

Hooks
═════
  • SalesOrder transitions to FULFILLED → informational log ONLY.
    ⚠ Stock deduction for orders no longer happens here. It moved to the
    single deduction point in apps/invoices — when the order's Invoice
    document is finalized (apps/invoices/signals.py →
    apps/invoices/stock.deduct_stock_for_invoice). Both standalone
    invoices and order-generated invoices pass through the same
    finalize_invoice code path, so deducting there covers both flows
    exactly once. Keeping a debit here as well would deduct the same
    goods twice.

  • PurchaseRequest transitions to DELIVERED → credit stock. We don't
    know the line items unless they were saved on the PR; the helper
    `services.on_purchase_delivered()` accepts an explicit `lines` arg
    so a finance view (or future receiving form) can call it with what
    was actually delivered.

Wired by InventoryConfig.ready().
"""
from __future__ import annotations

import logging

from django.db.models.signals import post_save
from django.dispatch import receiver

logger = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════════════════════
# Sales order: when fulfilled → log only (deduction happens at invoice
# finalize — see module docstring)
# ════════════════════════════════════════════════════════════════════════════

try:
    from apps.orders.models import SalesOrder, OrderStatus  # type: ignore
    HAVE_ORDERS = True
except Exception:
    HAVE_ORDERS = False


if HAVE_ORDERS:
    # Track previous status so we only fire on the new fulfillment edge.
    _PRE_SAVE_STATUS: dict = {}

    from django.db.models.signals import pre_save  # noqa: E402

    @receiver(pre_save, sender=SalesOrder, dispatch_uid='inventory.cache_order_status')
    def _cache_order_status(sender, instance, **kwargs):
        if not instance.pk:
            return
        try:
            prev = sender.objects.only('status').get(pk=instance.pk).status
        except sender.DoesNotExist:
            return
        _PRE_SAVE_STATUS[str(instance.pk)] = prev

    @receiver(post_save, sender=SalesOrder, dispatch_uid='inventory.on_order_fulfilled')
    def _on_order_fulfilled(sender, instance, created, **kwargs):
        if created:
            return
        prev = _PRE_SAVE_STATUS.pop(str(instance.pk), None)
        if instance.status != OrderStatus.FULFILLED:
            return
        if prev == OrderStatus.FULFILLED:
            return  # no-op — same status

        # Stock for this order was already deducted when its Invoice
        # document was finalized (apps/invoices/stock.py). Nothing to
        # debit here — just leave a breadcrumb for the audit trail.
        logger.info(
            'Order %s fulfilled. Stock was deducted at invoice finalize; '
            'no additional deduction performed.',
            getattr(instance, 'order_no', instance.pk),
        )


# ════════════════════════════════════════════════════════════════════════════
# Purchase request: when delivered → log informational event only
# ════════════════════════════════════════════════════════════════════════════
#
# We deliberately do NOT auto-receive line items here, because the project's
# PurchaseRequest model (apps/finance/models.py) doesn't store SKU-mapped
# line items — only a free-text description. Auto-receiving would require
# guessing what was delivered.
#
# Instead, we expose services.on_purchase_delivered(...) as a callable
# from a future "Receive Goods" view in inventory: the storekeeper picks
# the products, batches and quantities they actually unboxed, and that
# view calls the bridge.
#
# If you later add structured line items to PurchaseRequest, replace this
# block with a real receiver that walks them.

try:
    from apps.finance.models import PurchaseRequest  # type: ignore

    @receiver(post_save, sender=PurchaseRequest, dispatch_uid='inventory.on_purchase_delivered')
    def _on_purchase_delivered(sender, instance, created, **kwargs):
        if created:
            return
        if str(getattr(instance, 'status', '')).lower() != 'delivered':
            return
        # Just log it — receiving form will do the actual work.
        logger.info(
            'PurchaseRequest %s marked as delivered. '
            'Awaiting receiving form to register stock inflow.',
            instance.pk,
        )
except Exception:  # pragma: no cover
    pass