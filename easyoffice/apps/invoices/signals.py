"""
apps/invoices/signals.py
=========================

Listens for InvoiceDocument transitions and keeps the linked finance-app
records in sync.

Currently:
  - When an invoice transitions to VOIDED, cancel the linked
    IncomingPaymentRequest (only if it isn't already paid — paid
    receivables represent real revenue and shouldn't be retroactively
    cancelled; that's a refund flow, not a void).
  - When a document of type INVOICE transitions to FINALIZED, deduct
    inventory stock for line items that match inventory products.
    Deduction is clamped to available stock (out-of-stock items are
    never blocked and never deducted), free-text lines are skipped, and
    proformas / delivery notes never touch stock. This is the SINGLE
    deduction point for both standalone invoices and order-generated
    invoices (the order chain finalizes its Invoice document through the
    same code path). See apps/invoices/stock.py.

Wire-up: make sure apps/invoices/apps.py has a `ready()` that imports
this module:

    # apps/invoices/apps.py
    from django.apps import AppConfig

    class InvoicesConfig(AppConfig):
        default_auto_field = 'django.db.models.BigAutoField'
        name = 'apps.invoices'

        def ready(self):
            from . import signals  # noqa: F401
"""
import logging

from django.db.models.signals import pre_save, post_save
from django.dispatch import receiver

from .models import InvoiceDocument, DocType

log = logging.getLogger(__name__)


@receiver(pre_save, sender=InvoiceDocument)
def _cache_old_invoice_status(sender, instance, **kwargs):
    """
    Stash the previous status so post_save can detect a finalized → voided
    transition without us having to plumb it through every void code path.
    """
    if instance.pk is None:
        instance._previous_status = None
        return
    try:
        old = InvoiceDocument.objects.only('status').get(pk=instance.pk)
        instance._previous_status = old.status
    except InvoiceDocument.DoesNotExist:
        instance._previous_status = None


@receiver(post_save, sender=InvoiceDocument)
def _on_invoice_voided_cancel_receivable(sender, instance, created, **kwargs):
    """
    When an invoice is voided, cancel the linked receivable so it stops
    showing up in AR ageing / unpaid lists. Paid receivables are NOT
    touched (they're real revenue — refunds are a separate flow).
    """
    if created:
        return

    prev = getattr(instance, '_previous_status', None)
    if (prev != InvoiceDocument.Status.VOIDED
            and instance.status == InvoiceDocument.Status.VOIDED):
        try:
            from .services import sync_invoice_payment_state
            sync_invoice_payment_state(instance)
        except Exception:
            log.exception(
                'Failed to sync receivable state for voided invoice %s',
                instance.pk,
            )


@receiver(post_save, sender=InvoiceDocument)
def _on_invoice_finalized_deduct_stock(sender, instance, created, **kwargs):
    """
    Draft → FINALIZED transition on a document of type INVOICE → deduct
    inventory stock for matching line items.

    Only INVOICE documents deduct — a Proforma is a quotation and a
    Delivery Note is issued for goods already invoiced, so hooking either
    would double-count. Fires exactly once per invoice (finalize is a
    one-way, single-shot transition). Fail-soft: any error here is logged
    and never blocks or rolls back the finalize.
    """
    if created:
        return
    if instance.doc_type != DocType.INVOICE:
        return

    prev = getattr(instance, '_previous_status', None)
    if (prev == InvoiceDocument.Status.FINALIZED
            or instance.status != InvoiceDocument.Status.FINALIZED):
        return

    try:
        from .stock import deduct_stock_for_invoice
        actor = getattr(instance, 'finalized_by', None) or instance.created_by
        report = deduct_stock_for_invoice(instance, actor=actor)
        if report.get('shortfall'):
            log.warning(
                'Invoice %s finalized with stock shortfalls: %s',
                instance.number or instance.pk, report['shortfall'],
            )
    except Exception:
        log.exception(
            'Stock deduction failed for finalized invoice %s',
            instance.pk,
        )