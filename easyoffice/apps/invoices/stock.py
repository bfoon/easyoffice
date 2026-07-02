"""
apps/invoices/stock.py
=======================

Stock deduction for finalized invoices — the single deduction point for
BOTH flows:

    • Standalone invoices (invoice generator)      → deduct on finalize
    • Order-generated invoices (apps.orders chain) → the order's Invoice
      document also goes through finalize_invoice, so it deducts here too.
      (The old deduct-on-FULFILLED hook in apps/inventory/signals.py must
      be retired to avoid double deduction — see that file.)

Rules
─────
  • Only documents of type INVOICE deduct. Proformas and Delivery Notes
    never touch stock.
  • A line item deducts only if it matches an inventory Product
    (exact name or SKU match, case-insensitive — this is what the
    builder autocomplete fills in). Free-text lines are skipped.
  • Only kind=STOCKED products deduct. Services / bundles are skipped.
  • Deduction is CLAMPED to what is actually available:
        deduct = min(line quantity, available on hand)
    If a product is out of stock, nothing is deducted and the invoice
    still finalizes normally. Shortfalls are logged, never raised.
  • Everything goes through the inventory services layer so each
    deduction lands as a proper auditable StockMovement — this module
    NEVER writes StockItem quantities directly.

Fail-soft by design: any problem here logs and returns a report; it can
never block or roll back an invoice finalize.
"""
from __future__ import annotations

import inspect
import logging
from decimal import Decimal

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Lazy inventory access
# ─────────────────────────────────────────────────────────────────────────────

def _inventory():
    """Return (models_module, services_module) or (None, None)."""
    try:
        from apps.inventory import models as inv_models
        from apps.inventory import services as inv_services
        return inv_models, inv_services
    except Exception:
        return None, None


def _match_product(inv_models, description: str):
    """
    Resolve an invoice line description to a Product.
    Matches exact name or exact SKU (case-insensitive). Returns None for
    free-text lines — those simply don't deduct.
    """
    desc = (description or '').strip()
    if not desc:
        return None
    Product = inv_models.Product
    from django.db.models import Q
    return (
        Product.objects
        .filter(is_active=True)
        .filter(Q(name__iexact=desc) | Q(sku__iexact=desc))
        .first()
    )


# ─────────────────────────────────────────────────────────────────────────────
# Ledger adapter
# ─────────────────────────────────────────────────────────────────────────────
#
# We call the inventory services layer by name so every deduction produces a
# real StockMovement. Candidate function names are tried in order and their
# signatures are inspected, so this adapts to the exact services API without
# this module needing to know it at import time. If nothing binds, we log
# loudly and deduct NOTHING (never a silent partial ledger).

_ISSUE_CANDIDATES = ('issue_stock', 'stock_issue', 'issue')
_ADJUST_CANDIDATES = ('adjust_stock', 'stock_adjust')

# How our values map onto plausible parameter names in the services layer.
_PARAM_ALIASES = {
    'product':  ('product', 'prod'),
    'location': ('location', 'from_location', 'source_location', 'loc'),
    'quantity': ('quantity', 'qty', 'amount'),
    'delta':    ('delta', 'change', 'quantity_change'),
    'actor':    ('actor', 'user', 'by', 'created_by', 'performed_by'),
    'note':     ('note', 'notes', 'reason', 'memo', 'description'),
    'reference': ('reference', 'ref', 'source_ref', 'reference_number'),
    'source_kind': ('source_kind',),
}


def _bind_call(fn, values: dict):
    """
    Try to build kwargs for `fn` from `values` using the alias table.
    Returns kwargs dict if every required parameter is satisfied, else None.
    """
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return None

    kwargs = {}
    for pname, param in sig.parameters.items():
        if param.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
            continue
        supplied = False
        for our_key, aliases in _PARAM_ALIASES.items():
            if pname in aliases and our_key in values:
                kwargs[pname] = values[our_key]
                supplied = True
                break
        if not supplied and param.default is inspect.Parameter.empty:
            return None  # required param we can't satisfy
    return kwargs


def _ledger_issue(inv_services, *, product, location, quantity: Decimal,
                  actor, note: str, reference: str) -> bool:
    """
    Record one stock issue through the services layer.
    Returns True on success, False if no service function could be bound.
    """
    base = {
        'product': product, 'location': location, 'actor': actor,
        'note': note, 'reference': reference, 'source_kind': 'invoice',
    }

    for name in _ISSUE_CANDIDATES:
        fn = getattr(inv_services, name, None)
        if not callable(fn):
            continue
        kwargs = _bind_call(fn, {**base, 'quantity': quantity})
        if kwargs is not None:
            fn(**kwargs)
            return True

    # Fall back to a negative adjustment (adjust_stock handles negative
    # deltas as ADJUST_OUT movements — same mechanism the Odoo importer uses).
    for name in _ADJUST_CANDIDATES:
        fn = getattr(inv_services, name, None)
        if not callable(fn):
            continue
        kwargs = _bind_call(fn, {**base, 'quantity': -quantity, 'delta': -quantity})
        if kwargs is not None:
            fn(**kwargs)
            return True

    return False


# ─────────────────────────────────────────────────────────────────────────────
# Main entry point
# ─────────────────────────────────────────────────────────────────────────────

def deduct_stock_for_invoice(invoice, actor=None) -> dict:
    """
    Deduct stock for a finalized INVOICE document. Called from
    apps/invoices/signals.py on the draft → finalized transition.

    Returns a report dict:
        {
          'deducted':  [{sku, name, qty, locations: [...]}, ...],
          'shortfall': [{sku, name, asked, available}, ...],
          'skipped':   [descriptions that matched nothing],   # free text
          'errors':    [messages],
        }
    """
    report = {'deducted': [], 'shortfall': [], 'skipped': [], 'errors': []}

    inv_models, inv_services = _inventory()
    if inv_models is None:
        report['errors'].append('inventory app unavailable')
        return report

    ref = invoice.number or str(invoice.pk)
    Product = inv_models.Product

    for line in invoice.items.all():
        desc = (line.description or '').strip()
        qty = line.quantity or Decimal('0')
        if not desc or qty <= 0:
            continue

        product = _match_product(inv_models, desc)
        if product is None:
            report['skipped'].append(desc)          # free-text line — fine
            continue
        if product.kind != Product.Kind.STOCKED:
            report['skipped'].append(f'{desc} (service/bundle)')
            continue

        available = product.total_available
        to_deduct = min(qty, max(available, Decimal('0')))

        if to_deduct <= 0:
            report['shortfall'].append({
                'sku': product.sku, 'name': product.name,
                'asked': str(qty), 'available': str(available),
            })
            logger.warning(
                'Invoice %s: "%s" is out of stock (asked %s, available %s) — '
                'no deduction made.', ref, product.name, qty, available,
            )
            continue

        if to_deduct < qty:
            report['shortfall'].append({
                'sku': product.sku, 'name': product.name,
                'asked': str(qty), 'available': str(available),
            })
            logger.warning(
                'Invoice %s: partial stock for "%s" — deducting %s of %s.',
                ref, product.name, to_deduct, qty,
            )

        # Walk this product's per-location stock, fullest location first,
        # issuing from each until the clamped quantity is covered.
        remaining = to_deduct
        touched_locations = []
        stock_items = (
            product.stock_items
            .filter(quantity__gt=0)
            .select_related('location')
            .order_by('-quantity')
        )
        try:
            for si in stock_items:
                if remaining <= 0:
                    break
                take = min(remaining, si.quantity)
                ok = _ledger_issue(
                    inv_services,
                    product=product,
                    location=si.location,
                    quantity=take,
                    actor=actor,
                    note=f'Sold on invoice {ref}',
                    reference=ref,
                )
                if not ok:
                    report['errors'].append(
                        'No compatible issue/adjust function found in '
                        'apps.inventory.services — stock NOT deducted.'
                    )
                    logger.error(
                        'Invoice %s: could not bind an inventory services '
                        'function for stock deduction. Nothing was deducted '
                        'for "%s".', ref, product.name,
                    )
                    remaining = to_deduct  # nothing happened
                    break
                remaining -= take
                touched_locations.append(getattr(si.location, 'name', str(si.location_id)))
        except Exception as exc:
            report['errors'].append(f'{product.name}: {exc}')
            logger.exception(
                'Invoice %s: stock deduction failed for "%s"', ref, product.name,
            )
            continue

        done = to_deduct - remaining
        if done > 0:
            report['deducted'].append({
                'sku': product.sku, 'name': product.name,
                'qty': str(done), 'locations': touched_locations,
            })

    if report['deducted'] or report['shortfall'] or report['errors']:
        logger.info(
            'Invoice %s stock deduction — deducted: %d line(s), '
            'shortfalls: %d, skipped free-text: %d, errors: %d',
            ref, len(report['deducted']), len(report['shortfall']),
            len(report['skipped']), len(report['errors']),
        )
    return report
