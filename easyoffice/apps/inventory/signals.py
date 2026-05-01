"""
apps/inventory/signals.py
─────────────────────────
Bridges from other apps into the inventory ledger.

Hooks
═════
  • SalesOrder transitions to FULFILLED → debit stock at the default
    sellable location (orders.signals already flips the status; we
    listen to the same post_save event and trigger after).

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
# Sales order: when fulfilled → debit stock
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

        from . import services
        try:
            services.on_sales_order_fulfilled(instance, actor=None)
        except Exception:
            logger.exception(
                'Inventory hook failed for fulfilled order %s',
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
