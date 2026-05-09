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

from .models import InvoiceDocument

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
