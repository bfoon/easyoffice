"""
apps/pos/inventory.py  —  v2, native to apps.inventory
──────────────────────────────────────────────────────
Adapter between the POS and the inventory app (Product / StockItem /
StockMovement / Location).

What changed from v1 (the settings-driven generic adapter):

• SALES WRITE REAL LEDGER MOVEMENTS. Completing a sale goes through
  apps.inventory.services.issue_stock(kind='sale') when available, so
  every till sale appears in the movement list, stock reports, and
  stock-take reconciliation exactly like any other issue — tagged
  source_kind='pos_sale' with the receipt number as source_ref. If the
  service layer is unavailable the adapter falls back to an equivalent
  atomic ledger write of its own (StockMovement + StockItem update under
  select_for_update).

• VOIDS ARE RETURN_IN MOVEMENTS, not silent quantity bumps — the audit
  trail shows the void referencing the same receipt number.

• PER-LOCATION STOCK. The till sells from ONE location, resolved as:
      1. settings.POS_LOCATION_CODE            (e.g. 'MAIN-SHOP')
      2. first active Location(kind='shop', is_sellable=True)
      3. first active Location(is_sellable=True)
      4. first active Location
  Stock shown on the terminal is *available* at that location
  (quantity − reserved), so units reserved for pending orders can't be
  sold out from under the Orders flow.

• SCANNING UNDERSTANDS ALL YOUR CODES, in priority order:
      1. Product.barcode    (exact — the EAN/UPC path)
      2. Product.sku        (case-insensitive — typed codes)
      3. Product.qr_token   (your printed product labels)
      4. StockItem.qr_token (shelf/bin labels → resolves to the product)
  Camera scans of label QRs that encode a full URL
  (…/inventory/q/<token>/) are handled too — the token is extracted.

Settings (all optional):

    POS_LOCATION_CODE  = 'MAIN-SHOP'  # which Location the till sells from
    POS_ALLOW_OVERSELL = False        # True lets available stock go negative
"""
from __future__ import annotations

import inspect
import logging
import re
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone

logger = logging.getLogger(__name__)

# Matches the trailing token of a scanned label URL: …/q/<token>/
_QR_URL_RE = re.compile(r'/q/(?P<token>[A-Za-z0-9_\-]{6,40})/?\s*$')


def allow_oversell() -> bool:
    return bool(getattr(settings, 'POS_ALLOW_OVERSELL', False))


# ─────────────────────────────────────────────────────────────────────────────
# Lazy model access (house style — avoids circular imports at app load)
# ─────────────────────────────────────────────────────────────────────────────

def _models():
    from apps.inventory.models import (
        Product, StockItem, StockMovement, Location,
    )
    return Product, StockItem, StockMovement, Location


def _inv_services():
    try:
        from apps.inventory import services as inv_services
        return inv_services
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# POS location
# ─────────────────────────────────────────────────────────────────────────────

def pos_location():
    """Resolve the Location this till sells from. Cached per-process."""
    if getattr(pos_location, '_cache', None) is not None:
        return pos_location._cache

    Product, StockItem, StockMovement, Location = _models()
    loc = None
    code = (getattr(settings, 'POS_LOCATION_CODE', '') or '').strip()
    if code:
        loc = Location.objects.filter(code__iexact=code, is_active=True).first()
        if loc is None:
            logger.warning('POS: POS_LOCATION_CODE=%r not found — falling back', code)
    if loc is None:
        loc = Location.objects.filter(
            kind='shop', is_sellable=True, is_active=True).first()
    if loc is None:
        loc = Location.objects.filter(is_sellable=True, is_active=True).first()
    if loc is None:
        loc = Location.objects.filter(is_active=True).first()

    pos_location._cache = loc
    return loc


def clear_location_cache():
    pos_location._cache = None


# ─────────────────────────────────────────────────────────────────────────────
# Reading
# ─────────────────────────────────────────────────────────────────────────────

def _available_at(product, location):
    """Available (= on-hand − reserved) at the till's location. None for services."""
    if getattr(product, 'kind', 'stocked') == 'service':
        return None
    if location is None:
        return Decimal('0')
    row = product.stock_items.filter(location=location).first()
    if row is None:
        return Decimal('0')
    return (row.quantity or Decimal('0')) - (row.reserved_quantity or Decimal('0'))


def serialize(product) -> dict:
    """Uniform product dict for the terminal."""
    loc = pos_location()
    avail = _available_at(product, loc)
    total = None
    if getattr(product, 'kind', 'stocked') != 'service':
        try:
            total = int(product.total_available)
        except Exception:
            total = None
    return {
        'inventory_id': str(product.pk),
        'name':    product.name[:255],
        'sku':     product.sku or '',
        'barcode': product.barcode or '',
        'price':   str(product.sell_price or Decimal('0.00')),
        'currency': product.currency or 'GMD',
        'kind':    product.kind,
        # what the terminal shows / warns on — stock at THIS till's location
        'stock':   int(avail) if avail is not None else None,
        'stock_total': total,   # across all locations (context only)
    }


def _sellable_qs():
    Product, *_ = _models()
    return Product.objects.filter(is_active=True, is_sellable=True)


def lookup_by_code(code: str):
    """
    The scan path. Resolves barcodes, SKUs, product QR tokens, shelf-label
    QR tokens, and full label URLs. Returns a product dict or None.
    """
    code = (code or '').strip()
    if not code:
        return None

    # Camera scan of a printed label often yields the full resolve URL.
    m = _QR_URL_RE.search(code)
    if m:
        code = m.group('token')

    Product, StockItem, *_ = _models()
    qs = _sellable_qs()

    product = (
        qs.filter(barcode=code).first()
        or qs.filter(sku__iexact=code).first()
        or qs.filter(qr_token=code).first()
    )
    if product is None:
        # Shelf/bin label — StockItem QR resolves to its product.
        si = (StockItem.objects
              .select_related('product')
              .filter(qr_token=code,
                      product__is_active=True,
                      product__is_sellable=True)
              .first())
        product = si.product if si else None

    return serialize(product) if product else None


def search(query: str, limit: int = 12) -> list:
    """Type-ahead on name / SKU / barcode — sellable products only."""
    query = (query or '').strip()
    if len(query) < 2:
        return []
    qs = _sellable_qs().filter(
        Q(name__icontains=query)
        | Q(sku__icontains=query)
        | Q(barcode__icontains=query)
    ).select_related('category')[:limit]
    return [serialize(p) for p in qs]


def get_item(inventory_id: str):
    if not inventory_id:
        return None
    try:
        return _sellable_qs().filter(pk=inventory_id).first()
    except (ValueError, TypeError):
        return None


def current_stock(inventory_id: str):
    product = get_item(inventory_id)
    if product is None:
        return None
    avail = _available_at(product, pos_location())
    return int(avail) if avail is not None else None


# ─────────────────────────────────────────────────────────────────────────────
# Writing — every quantity change is a StockMovement
# ─────────────────────────────────────────────────────────────────────────────

def _call_service(fn, **kwargs):
    """Call an inventory service, passing only kwargs its signature accepts."""
    accepted = set(inspect.signature(fn).parameters)
    return fn(**{k: v for k, v in kwargs.items() if k in accepted})


def _ledger_write(product, location, *, kind: str, qty, actor=None,
                  source_ref: str = '', notes: str = '',
                  require_available: bool = False) -> bool:
    """
    Fallback path: atomic StockMovement + StockItem update, equivalent to
    what the inventory service layer does. `require_available` enforces
    (quantity − reserved) ≥ qty under select_for_update.
    """
    Product, StockItem, StockMovement, Location = _models()
    qty = Decimal(str(qty))

    with transaction.atomic():
        item, _ = (StockItem.objects
                   .select_for_update()
                   .get_or_create(product=product, location=location))

        sign = StockMovement.SIGN.get(kind, 0)
        if sign < 0 and require_available and not allow_oversell():
            avail = ((item.quantity or Decimal('0'))
                     - (item.reserved_quantity or Decimal('0')))
            if avail < qty:
                return False

        item.quantity = F('quantity') + (qty * sign)
        item.last_movement_at = timezone.now()
        item.save(update_fields=['quantity', 'last_movement_at', 'updated_at'])

        StockMovement.objects.create(
            product=product,
            location=location,
            kind=kind,
            quantity=qty,
            source_kind='pos_sale',
            source_ref=(source_ref or '')[:80],
            actor=actor if (actor and getattr(actor, 'is_authenticated', False)) else None,
            notes=(notes or '')[:300],
        )
    return True


def deduct_stock(inventory_id: str, qty: int, *, actor=None,
                 source_ref: str = '') -> bool:
    """
    Sell `qty` of a product from the till's location as a SALE movement.
    Returns False when available stock is insufficient and oversell is off.
    Service products (no stock) always succeed without a movement.
    """
    product = get_item(inventory_id)
    if product is None:
        # Product went inactive between scan and completion — treat as
        # "nothing to deduct" rather than blocking the sale of a snapshot.
        logger.warning('POS: deduct for unknown/inactive product %s', inventory_id)
        return True
    if getattr(product, 'kind', 'stocked') == 'service':
        return True

    location = pos_location()
    if location is None:
        logger.error('POS: no Location available — configure POS_LOCATION_CODE')
        return False

    # Preferred: the inventory app's own service layer (keeps ALL of its
    # semantics — batch handling, low-stock alerts, etc.). Signature-
    # filtered so optional params it doesn't accept are simply dropped.
    svc = _inv_services()
    if svc is not None and hasattr(svc, 'issue_stock'):
        try:
            _call_service(
                svc.issue_stock,
                product=product,
                location=location,
                quantity=Decimal(int(qty)),
                kind='sale',
                actor=actor,
                notes=f'POS sale {source_ref}'.strip(),
                source_kind='pos_sale',
                source_ref=source_ref,
                allow_oversell=allow_oversell(),
            )
            return True
        except Exception as exc:
            # Insufficient stock raised by the service → report as such;
            # anything import/signature-shaped falls through to the ledger.
            msg = str(exc).lower()
            if ('stock' in msg or 'quantity' in msg or 'available' in msg
                    or 'insufficient' in msg):
                return False
            logger.warning('POS: issue_stock failed (%s) — using ledger fallback', exc)

    return _ledger_write(
        product, location, kind='sale', qty=qty, actor=actor,
        source_ref=source_ref, notes='POS terminal sale',
        require_available=True,
    )


def restore_stock(inventory_id: str, qty: int, *, actor=None,
                  source_ref: str = '') -> None:
    """Void/refund → RETURN_IN movement at the till's location. Best-effort."""
    product = get_item(inventory_id)
    if product is None or getattr(product, 'kind', 'stocked') == 'service':
        return
    location = pos_location()
    if location is None:
        return
    try:
        _ledger_write(
            product, location, kind='return_in', qty=qty, actor=actor,
            source_ref=source_ref, notes='POS sale voided — stock restored',
        )
    except Exception:
        logger.exception('POS: restore_stock failed for %s (+%s)', inventory_id, qty)
