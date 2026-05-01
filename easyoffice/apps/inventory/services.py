"""
apps/inventory/services.py
──────────────────────────
Business logic for inventory. Views stay thin; everything that touches
multiple models or has policy implications lives here.

Public entry points
═══════════════════

  ┌─ Stock movements ────────────────────────────────────────────────┐
  │ receive_stock(...)         — vendor delivery / restock           │
  │ issue_stock(...)           — generic outbound (returns Movement) │
  │ transfer_stock(...)        — location-to-location                │
  │ adjust_stock(...)          — stock-take / damage / found         │
  │ scrap_stock(...)           — write-off                           │
  └──────────────────────────────────────────────────────────────────┘

  ┌─ Reservations (gentle holds) ────────────────────────────────────┐
  │ reserve_for_order(order, ...)   — when order moves to PENDING    │
  │ release_reservation(order, ...) — on cancel / return             │
  │ commit_reservation(order, ...)  — on fulfillment (turns into OUT)│
  └──────────────────────────────────────────────────────────────────┘

  ┌─ Asset lifecycle ────────────────────────────────────────────────┐
  │ assign_asset(...)          / return_asset(...)                   │
  │ transfer_asset(...)        / change_asset_status(...)            │
  │ scrap_asset(...)                                                 │
  └──────────────────────────────────────────────────────────────────┘

  ┌─ Stock requests (internal) ──────────────────────────────────────┐
  │ submit_stock_request(...)  / issue_stock_request(...)            │
  │ reroute_to_purchase(...)   — auto-create finance.PurchaseRequest │
  └──────────────────────────────────────────────────────────────────┘

  ┌─ Bridges from other apps ────────────────────────────────────────┐
  │ on_sales_order_fulfilled(order) — called by orders.signals       │
  │ on_purchase_delivered(pr, items)— called by finance signals      │
  └──────────────────────────────────────────────────────────────────┘

Every state change writes:
  1. a row in StockMovement / AssetEvent (audit trail)
  2. an updated counter on StockItem / Asset
  3. a notification (post-commit, via notifications.py)
"""
from __future__ import annotations

import logging
import uuid
from decimal import Decimal
from typing import Iterable, Optional

from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone

from .models import (
    Asset, AssetAssignment, AssetEvent, AssetMaintenance,
    Location, Product, StockBatch, StockItem, StockMovement,
    StockRequest, StockRequestLine, StockTake, StockTakeLine,
    Supplier,
)
from . import notifications

logger = logging.getLogger(__name__)
User = get_user_model()


# ════════════════════════════════════════════════════════════════════════════
# Internal helpers
# ════════════════════════════════════════════════════════════════════════════

def _get_or_create_stock_item(product: Product, location: Location) -> StockItem:
    """One row per (product, location). Created lazily on first movement."""
    si, _ = StockItem.objects.get_or_create(
        product=product, location=location,
        defaults={'quantity': Decimal('0.00'),
                  'reserved_quantity': Decimal('0.00')},
    )
    return si


def _apply_movement_to_stock(stock_item: StockItem, delta: Decimal):
    """
    Apply a signed delta to the on-hand quantity of a StockItem.

    Note: we DO allow the resulting quantity to go negative. This matches
    real life where shrinkage is sometimes recorded after the fact, and
    sales staff might issue stock that was physically there but not yet
    in the system. Negative balances surface in the dashboard as a red
    flag and prompt a stock-take.
    """
    StockItem.objects.filter(pk=stock_item.pk).update(
        quantity=stock_item.quantity + delta,
        last_movement_at=timezone.now(),
    )
    stock_item.refresh_from_db(fields=['quantity', 'last_movement_at'])


def _check_low_stock_after(product: Product, *, actor=None):
    """
    Schedule a low-stock alert if the new total is at or below the
    re-order point. Called after every outbound movement.
    """
    if product.kind != Product.Kind.STOCKED:
        return
    if not product.reorder_point or product.reorder_point <= 0:
        return
    available = product.total_available
    if available <= product.reorder_point:
        # Notifications run at end of transaction so they see the
        # latest stock figures.
        transaction.on_commit(
            lambda: notifications.notify_low_stock(product, actor=actor)
        )


# ════════════════════════════════════════════════════════════════════════════
# Stock movements (the primitive operations)
# ════════════════════════════════════════════════════════════════════════════

@transaction.atomic
def receive_stock(
    *,
    product: Product,
    location: Location,
    quantity: Decimal | str | int,
    actor=None,
    cost_price: Optional[Decimal] = None,
    batch_no: str = '',
    expiry_date=None,
    supplier: Optional[Supplier] = None,
    source_kind: str = '',
    source_ref: str = '',
    notes: str = '',
) -> StockMovement:
    """
    Bring stock IN from a supplier or another source.

    If the product has track_batch == True, a batch row is created (or
    quantity added to an existing one with the same batch_no).
    """
    qty = Decimal(str(quantity))
    if qty <= 0:
        raise ValueError('Receive quantity must be positive.')

    si = _get_or_create_stock_item(product, location)

    batch = None
    if product.track_batch:
        if not batch_no:
            batch_no = f'B-{timezone.now():%Y%m%d%H%M%S}'
        batch, created = StockBatch.objects.get_or_create(
            stock_item=si, batch_no=batch_no,
            defaults={
                'received_at': timezone.now().date(),
                'expiry_date': expiry_date,
                'cost_price':  cost_price,
                'supplier':    supplier,
            },
        )
        if not created:
            batch.quantity = (batch.quantity or Decimal('0')) + qty
            batch.save(update_fields=['quantity'])
        else:
            batch.quantity = qty
            batch.save(update_fields=['quantity'])

    mv = StockMovement.objects.create(
        product=product, location=location, batch=batch,
        kind=StockMovement.Kind.RECEIVE,
        quantity=qty, actor=actor,
        source_kind=source_kind, source_ref=source_ref, notes=notes[:300],
    )
    _apply_movement_to_stock(si, qty)

    # Roll new cost price into the product if one was supplied
    if cost_price is not None and Decimal(str(cost_price)) > 0:
        Product.objects.filter(pk=product.pk).update(cost_price=Decimal(str(cost_price)))

    transaction.on_commit(
        lambda: notifications.notify_stock_received(product, location, qty, actor=actor)
    )
    return mv


@transaction.atomic
def issue_stock(
    *,
    product: Product,
    location: Location,
    quantity: Decimal | str | int,
    kind: str = StockMovement.Kind.SALE,
    actor=None,
    source_kind: str = '',
    source_ref: str = '',
    notes: str = '',
    allow_oversell: bool = True,
) -> StockMovement:
    """
    Outbound movement — sale, internal use, scrap, etc.

    `allow_oversell=True` means we'll record a movement that takes the
    on-hand quantity negative; this matches the pragmatic shop reality
    where stock has been physically given but the system was behind.
    Set False when you want to force-fail (e.g. an automated API).
    """
    qty = Decimal(str(quantity))
    if qty <= 0:
        raise ValueError('Issue quantity must be positive.')

    si = _get_or_create_stock_item(product, location)

    if not allow_oversell and si.available < qty:
        raise ValueError(
            f'Not enough {product.sku} at {location.code}: '
            f'{si.available} available, {qty} requested.'
        )

    mv = StockMovement.objects.create(
        product=product, location=location,
        kind=kind, quantity=qty, actor=actor,
        source_kind=source_kind, source_ref=source_ref, notes=notes[:300],
    )
    _apply_movement_to_stock(si, -qty)
    _check_low_stock_after(product, actor=actor)
    return mv


@transaction.atomic
def transfer_stock(
    *,
    product: Product,
    from_location: Location,
    to_location: Location,
    quantity: Decimal | str | int,
    actor=None,
    notes: str = '',
) -> tuple[StockMovement, StockMovement]:
    """
    Move stock between locations. Two linked movements share a
    transfer_group UUID so they can be reconciled later.
    """
    if from_location.pk == to_location.pk:
        raise ValueError('Transfer source and destination must differ.')

    qty = Decimal(str(quantity))
    if qty <= 0:
        raise ValueError('Transfer quantity must be positive.')

    group = uuid.uuid4()

    out_mv = StockMovement.objects.create(
        product=product, location=from_location,
        kind=StockMovement.Kind.TRANSFER_OUT, quantity=qty, actor=actor,
        transfer_group=group, source_kind='transfer', notes=notes[:300],
    )
    in_mv = StockMovement.objects.create(
        product=product, location=to_location,
        kind=StockMovement.Kind.TRANSFER_IN,  quantity=qty, actor=actor,
        transfer_group=group, source_kind='transfer', notes=notes[:300],
    )

    _apply_movement_to_stock(_get_or_create_stock_item(product, from_location), -qty)
    _apply_movement_to_stock(_get_or_create_stock_item(product, to_location),   +qty)
    _check_low_stock_after(product, actor=actor)

    transaction.on_commit(
        lambda: notifications.notify_stock_transferred(
            product, from_location, to_location, qty, actor=actor,
        )
    )
    return out_mv, in_mv


@transaction.atomic
def adjust_stock(
    *,
    product: Product,
    location: Location,
    delta: Decimal | str | int,
    actor=None,
    notes: str = '',
    source_kind: str = 'adjustment',
    source_ref: str = '',
) -> StockMovement:
    """
    Manual correction — counted vs system. Positive `delta` adds, negative
    removes. Used by stock-take finalisation and for one-off fixes.
    """
    delta = Decimal(str(delta))
    if delta == 0:
        raise ValueError('Adjustment delta cannot be zero.')

    kind = (StockMovement.Kind.ADJUST_IN if delta > 0
            else StockMovement.Kind.ADJUST_OUT)

    mv = StockMovement.objects.create(
        product=product, location=location,
        kind=kind, quantity=abs(delta), actor=actor,
        source_kind=source_kind, source_ref=source_ref, notes=notes[:300],
    )
    _apply_movement_to_stock(_get_or_create_stock_item(product, location), delta)
    if delta < 0:
        _check_low_stock_after(product, actor=actor)
    return mv


@transaction.atomic
def scrap_stock(
    *,
    product: Product,
    location: Location,
    quantity: Decimal | str | int,
    reason: str,
    actor=None,
) -> StockMovement:
    """Write-off. Same shape as issue but marked SCRAP for reporting."""
    return issue_stock(
        product=product, location=location, quantity=quantity,
        kind=StockMovement.Kind.SCRAP, actor=actor,
        source_kind='scrap', notes=reason,
    )


# ════════════════════════════════════════════════════════════════════════════
# Reservations (soft-holds for pending orders)
# ════════════════════════════════════════════════════════════════════════════

@transaction.atomic
def reserve_for_order(
    *,
    product: Product,
    location: Location,
    quantity: Decimal | str | int,
    source_ref: str,
):
    """
    Bump StockItem.reserved_quantity. Does NOT create a movement — the
    physical stock hasn't moved yet. Call commit_reservation() to flush.
    """
    qty = Decimal(str(quantity))
    si = _get_or_create_stock_item(product, location)
    StockItem.objects.filter(pk=si.pk).update(
        reserved_quantity=si.reserved_quantity + qty,
    )
    logger.info(
        'Reserved %s of %s at %s for ref=%s',
        qty, product.sku, location.code, source_ref,
    )


@transaction.atomic
def release_reservation(
    *,
    product: Product,
    location: Location,
    quantity: Decimal | str | int,
):
    """Cancel a hold, e.g. on order cancellation."""
    qty = Decimal(str(quantity))
    si = _get_or_create_stock_item(product, location)
    new_reserved = max(si.reserved_quantity - qty, Decimal('0'))
    StockItem.objects.filter(pk=si.pk).update(reserved_quantity=new_reserved)


@transaction.atomic
def commit_reservation(
    *,
    product: Product,
    location: Location,
    quantity: Decimal | str | int,
    actor=None,
    source_ref: str = '',
) -> StockMovement:
    """
    Convert a reservation into an actual sale movement. Releases the
    reservation and decrements on-hand stock.
    """
    qty = Decimal(str(quantity))
    si = _get_or_create_stock_item(product, location)
    new_reserved = max(si.reserved_quantity - qty, Decimal('0'))
    StockItem.objects.filter(pk=si.pk).update(reserved_quantity=new_reserved)

    return issue_stock(
        product=product, location=location, quantity=qty,
        kind=StockMovement.Kind.SALE, actor=actor,
        source_kind='sales_order', source_ref=source_ref,
        allow_oversell=True,
    )


# ════════════════════════════════════════════════════════════════════════════
# Asset lifecycle
# ════════════════════════════════════════════════════════════════════════════

@transaction.atomic
def assign_asset(asset: Asset, *, assignee, actor=None, purpose: str = '') -> AssetAssignment:
    """
    Hand an asset to a staff member. Closes any open assignment first,
    then opens a new one. Asset.status is forced to ASSIGNED.
    """
    asset = Asset.objects.select_for_update().get(pk=asset.pk)
    if asset.status in (Asset.Status.RETIRED, Asset.Status.DISPOSED):
        raise ValueError('Cannot assign a retired or disposed asset.')

    # Auto-close any open assignment
    AssetAssignment.objects.filter(asset=asset, returned_at__isnull=True).update(
        returned_at=timezone.now(),
    )

    assignment = AssetAssignment.objects.create(
        asset=asset, assignee=assignee, assigned_by=actor,
        purpose=purpose[:300],
    )
    Asset.objects.filter(pk=asset.pk).update(
        assigned_to=assignee, status=Asset.Status.ASSIGNED,
    )
    AssetEvent.objects.create(
        asset=asset, kind=AssetEvent.Kind.ASSIGNED, actor=actor,
        message=f'Assigned to {assignee}',
        payload={'assignee_id': assignee.pk if hasattr(assignee, 'pk') else None,
                 'purpose': purpose[:200]},
    )
    transaction.on_commit(
        lambda: notifications.notify_asset_assigned(asset, assignee, actor=actor)
    )
    return assignment


@transaction.atomic
def return_asset(
    asset: Asset, *, actor=None, condition: str = '', note: str = '',
) -> Optional[AssetAssignment]:
    """Mark the open assignment as returned and put the asset back in stock."""
    asset = Asset.objects.select_for_update().get(pk=asset.pk)
    open_a = (AssetAssignment.objects
              .filter(asset=asset, returned_at__isnull=True)
              .order_by('-assigned_at').first())
    if not open_a:
        return None

    open_a.returned_at = timezone.now()
    open_a.return_condition = condition or ''
    open_a.return_note = note or ''
    open_a.save(update_fields=['returned_at', 'return_condition', 'return_note'])

    Asset.objects.filter(pk=asset.pk).update(
        assigned_to=None, status=Asset.Status.IN_STORE,
        condition=condition or asset.condition,
    )
    AssetEvent.objects.create(
        asset=asset, kind=AssetEvent.Kind.RETURNED, actor=actor,
        message=f'Returned by {open_a.assignee} ({condition or asset.condition})',
    )
    return open_a


@transaction.atomic
def transfer_asset(asset: Asset, *, to_location: Location, actor=None, note: str = '') -> Asset:
    asset = Asset.objects.select_for_update().get(pk=asset.pk)
    if asset.location_id == to_location.pk:
        return asset
    old_loc = asset.location
    asset.location = to_location
    asset.save(update_fields=['location', 'updated_at'])
    AssetEvent.objects.create(
        asset=asset, kind=AssetEvent.Kind.TRANSFERRED, actor=actor,
        message=f'{old_loc.code} → {to_location.code}: {note}'.strip(': '),
        payload={'from': old_loc.code, 'to': to_location.code},
    )
    return asset


@transaction.atomic
def change_asset_status(asset: Asset, *, status: str, actor=None, note: str = '') -> Asset:
    asset = Asset.objects.select_for_update().get(pk=asset.pk)
    if status == asset.status:
        return asset
    if status not in dict(Asset.Status.choices):
        raise ValueError(f'Invalid asset status: {status!r}')
    old = asset.status
    asset.status = status
    asset.save(update_fields=['status', 'updated_at'])
    AssetEvent.objects.create(
        asset=asset, kind=AssetEvent.Kind.STATUS, actor=actor,
        message=f'Status: {old} → {status} {note}'.strip(),
        payload={'from': old, 'to': status, 'note': note[:300]},
    )
    return asset


@transaction.atomic
def scrap_asset(asset: Asset, *, actor=None, reason: str = '') -> Asset:
    """Terminal — disposed / scrapped. Closes any open assignment."""
    asset = Asset.objects.select_for_update().get(pk=asset.pk)
    if asset.status == Asset.Status.DISPOSED:
        return asset
    AssetAssignment.objects.filter(asset=asset, returned_at__isnull=True).update(
        returned_at=timezone.now(),
        return_note=f'(asset disposed) {reason}'[:500],
    )
    asset.status = Asset.Status.DISPOSED
    asset.assigned_to = None
    asset.is_active = False
    asset.save(update_fields=['status', 'assigned_to', 'is_active', 'updated_at'])
    AssetEvent.objects.create(
        asset=asset, kind=AssetEvent.Kind.SCRAPPED, actor=actor,
        message=reason or 'Asset disposed.',
    )
    transaction.on_commit(
        lambda: notifications.notify_asset_scrapped(asset, actor=actor, reason=reason)
    )
    return asset


@transaction.atomic
def log_maintenance(
    asset: Asset, *,
    kind: str, description: str, cost: Decimal | str | int = 0,
    performed_by: str = '', performed_on=None, next_due=None,
    actor=None, attachment=None,
) -> AssetMaintenance:
    rec = AssetMaintenance.objects.create(
        asset=asset, kind=kind, description=description,
        cost=Decimal(str(cost or 0)), performed_by=performed_by,
        performed_on=performed_on or timezone.now().date(),
        next_due=next_due, recorded_by=actor, attachment=attachment,
    )
    AssetEvent.objects.create(
        asset=asset, kind=AssetEvent.Kind.MAINTENANCE, actor=actor,
        message=f'{rec.get_kind_display()}: {description[:200]}',
        payload={'cost': str(rec.cost), 'next_due': str(next_due) if next_due else ''},
    )
    return rec


# ════════════════════════════════════════════════════════════════════════════
# Stock requests (internal — staff asks for items)
# ════════════════════════════════════════════════════════════════════════════

@transaction.atomic
def submit_stock_request(
    *, requested_by, lines: list[dict],
    department=None, location=None, purpose: str = '',
) -> StockRequest:
    """
    Create a request and its lines. `lines` is a list of dicts with
    `product` (id or instance) and `quantity`.
    """
    if not lines:
        raise ValueError('A stock request must have at least one line.')

    sr = StockRequest.objects.create(
        requested_by=requested_by, department=department,
        location=location, purpose=purpose[:300],
        status=StockRequest.Status.PENDING,
    )

    for ln in lines:
        product = ln['product']
        if not isinstance(product, Product):
            product = Product.objects.get(pk=product)
        StockRequestLine.objects.create(
            request=sr, product=product,
            quantity=Decimal(str(ln.get('quantity') or 1)),
            note=str(ln.get('note', ''))[:200],
        )

    transaction.on_commit(lambda: notifications.notify_stock_request_submitted(sr))
    return sr


@transaction.atomic
def issue_stock_request(sr: StockRequest, *, actor, location: Optional[Location] = None) -> StockRequest:
    """
    Storekeeper issues items against a request. Movements are recorded
    line by line. Lines we can't fulfill are left unissued; if any line
    is short the request is marked PARTIAL.
    """
    sr = StockRequest.objects.select_for_update().get(pk=sr.pk)
    if sr.status not in {StockRequest.Status.PENDING, StockRequest.Status.APPROVED,
                         StockRequest.Status.PARTIAL}:
        raise ValueError(f'Cannot issue from a {sr.get_status_display()} request.')

    src_location = location or sr.location
    if not src_location:
        # Pick the first warehouse the system knows of
        src_location = Location.objects.filter(
            kind__in=[Location.Kind.WAREHOUSE, Location.Kind.SHOP],
            is_active=True,
        ).order_by('kind', 'name').first()
    if not src_location:
        raise ValueError('No source location available to issue from.')

    fully = True
    any_issued = False
    for line in sr.lines.select_related('product').all():
        remaining = (line.quantity or Decimal('0')) - (line.issued_qty or Decimal('0'))
        if remaining <= 0:
            continue
        si = _get_or_create_stock_item(line.product, src_location)
        give = min(remaining, max(si.available, Decimal('0')))
        if give <= 0:
            fully = False
            continue
        issue_stock(
            product=line.product, location=src_location, quantity=give,
            kind=StockMovement.Kind.INTERNAL_USE, actor=actor,
            source_kind='stock_request', source_ref=sr.reference,
            notes=f'Stock request {sr.reference}',
        )
        line.issued_qty = (line.issued_qty or Decimal('0')) + give
        line.save(update_fields=['issued_qty'])
        any_issued = True
        if line.issued_qty < line.quantity:
            fully = False

    if not any_issued:
        # Nothing in stock — caller should reroute_to_purchase
        raise ValueError('No requested items are currently in stock.')

    sr.status = StockRequest.Status.ISSUED if fully else StockRequest.Status.PARTIAL
    sr.issued_by = actor
    sr.issued_at = timezone.now()
    sr.save(update_fields=['status', 'issued_by', 'issued_at', 'updated_at'])

    transaction.on_commit(lambda: notifications.notify_stock_request_issued(sr, actor=actor))
    return sr


@transaction.atomic
def reroute_to_purchase(sr: StockRequest, *, actor) -> StockRequest:
    """
    Out of stock — create a finance.PurchaseRequest covering the unissued
    quantities and link it back. We import the finance model lazily to
    avoid a hard cross-app import at module load.
    """
    sr = StockRequest.objects.select_for_update().get(pk=sr.pk)

    try:
        from apps.finance.models import PurchaseRequest as FinancePR
    except Exception:
        logger.exception('Cannot import finance.PurchaseRequest — rerouting disabled.')
        raise ValueError('Purchase rerouting is not available — finance app is missing.')

    # Build a one-shot summary description from short lines
    short_lines = []
    estimated = Decimal('0.00')
    for line in sr.lines.select_related('product').all():
        remaining = (line.quantity or Decimal('0')) - (line.issued_qty or Decimal('0'))
        if remaining <= 0:
            continue
        short_lines.append(f'• {line.product.name} (SKU {line.product.sku}) ×{remaining}')
        estimated += remaining * (line.product.cost_price or Decimal('0'))

    if not short_lines:
        raise ValueError('Nothing left to purchase — request is already filled.')

    description = (
        f'Rerouted from internal stock request {sr.reference}. '
        f'Items requested but not in stock:\n\n' + '\n'.join(short_lines)
    )

    pr = FinancePR.objects.create(
        title=f'Stock request {sr.reference} — out of stock',
        description=description,
        requested_by=sr.requested_by,
        department=sr.department,
        estimated_cost=estimated or Decimal('0.00'),
        status=FinancePR.Status.SUBMITTED,
        priority=FinancePR.Priority.NORMAL,
        justification=(sr.purpose or 'Rerouted from inventory stock request.')[:5000],
    )

    sr.rerouted_to_purchase = pr.pk
    sr.rerouted_at = timezone.now()
    sr.status = StockRequest.Status.REROUTED
    sr.save(update_fields=[
        'rerouted_to_purchase', 'rerouted_at', 'status', 'updated_at',
    ])

    transaction.on_commit(lambda: notifications.notify_stock_request_rerouted(sr, pr, actor=actor))
    return sr


# ════════════════════════════════════════════════════════════════════════════
# Stock-take finalisation
# ════════════════════════════════════════════════════════════════════════════

@transaction.atomic
def finalise_stock_take(stock_take: StockTake, *, actor) -> StockTake:
    """
    Walk each line and emit ADJUST movements to reconcile counted vs
    expected. Marks the take as COMPLETED.
    """
    st = StockTake.objects.select_for_update().get(pk=stock_take.pk)
    if st.status == StockTake.Status.COMPLETED:
        return st
    if st.status == StockTake.Status.CANCELLED:
        raise ValueError('Cannot finalise a cancelled stock-take.')

    for line in st.lines.select_related('product').all():
        if line.counted is None:
            continue  # not counted, skip
        delta = line.variance
        if delta == 0:
            continue
        adjust_stock(
            product=line.product, location=st.location,
            delta=delta, actor=actor,
            source_kind='stock_take', source_ref=st.reference,
            notes=f'Stock-take {st.reference}: {line.note}'.strip(': '),
        )

    st.status = StockTake.Status.COMPLETED
    st.finalised_by = actor
    st.finalised_at = timezone.now()
    st.save(update_fields=['status', 'finalised_by', 'finalised_at'])
    transaction.on_commit(lambda: notifications.notify_stock_take_completed(st, actor=actor))
    return st


# ════════════════════════════════════════════════════════════════════════════
# Bridges from other apps
# ════════════════════════════════════════════════════════════════════════════

def on_sales_order_fulfilled(order, *, actor=None):
    """
    Called by apps/orders/signals.py when an order reaches FULFILLED.

    For each order line that maps to a stocked Product (matched by SKU
    or description), we issue stock from the first sellable location.
    Lines that don't match any product are skipped silently — the order
    might be a service.
    """
    try:
        # Pick a default sellable location — the first SHOP, then any
        sell_loc = (Location.objects
                    .filter(is_active=True, kind=Location.Kind.SHOP)
                    .order_by('name').first())
        if not sell_loc:
            sell_loc = (Location.objects
                        .filter(is_active=True, is_sellable=True)
                        .order_by('name').first())
        if not sell_loc:
            logger.warning('on_sales_order_fulfilled(%s): no sellable location configured.', order.pk)
            return

        for line in order.items.all():
            product = _match_product(line)
            if not product or product.kind != Product.Kind.STOCKED:
                continue
            try:
                issue_stock(
                    product=product, location=sell_loc,
                    quantity=line.quantity,
                    kind=StockMovement.Kind.SALE, actor=actor,
                    source_kind='sales_order', source_ref=order.order_no,
                    notes=f'Auto-debit on order {order.order_no} fulfillment.',
                    allow_oversell=True,
                )
            except Exception:
                logger.exception(
                    'Could not auto-issue stock for order %s line %s',
                    order.order_no, line.pk,
                )
    except Exception:
        logger.exception('on_sales_order_fulfilled failed for order %s', getattr(order, 'pk', None))


def _match_product(order_line) -> Optional[Product]:
    """
    Map an order line to a Product.

    Preferred path: the order line has a direct FK to inventory.Product
    (set when the agent picked from the inventory typeahead). If that's
    set, we trust it and return immediately.

    Fallback: order lines created before the FK existed, or via legacy
    integrations that only know the description, fall through to a
    best-effort search — leading SKU, exact name, then fuzzy contains.
    Returns None for free-text "custom" lines that don't match anything.
    """
    # Direct FK — set when the line was picked from the inventory typeahead
    direct = getattr(order_line, 'product', None)
    if direct is not None:
        return direct

    desc = (order_line.description or '').strip()
    if not desc:
        return None

    # 1. Try a leading SKU pattern: "SKU-123 — name"
    first_token = desc.split()[0] if desc else ''
    p = Product.objects.filter(sku__iexact=first_token).first()
    if p:
        return p
    # 2. Try exact name match
    p = Product.objects.filter(name__iexact=desc).first()
    if p:
        return p
    # 3. Try contains-based fuzzy
    return Product.objects.filter(name__icontains=desc[:60]).first()


def on_purchase_delivered(purchase_request, *, lines: Iterable[dict], to_location: Location, actor=None):
    """
    Called when finance.PurchaseRequest transitions to DELIVERED. `lines`
    is an iterable of dicts: {product, quantity, cost_price?, batch_no?}.
    Each one becomes a RECEIVE movement at `to_location`.
    """
    if not lines:
        logger.info('on_purchase_delivered(%s): no lines provided.', purchase_request.pk)
        return

    for ln in lines:
        product = ln['product']
        if not isinstance(product, Product):
            product = Product.objects.get(pk=product)
        try:
            receive_stock(
                product=product, location=to_location,
                quantity=ln['quantity'],
                cost_price=ln.get('cost_price'),
                batch_no=ln.get('batch_no', ''),
                expiry_date=ln.get('expiry_date'),
                supplier=ln.get('supplier'),
                actor=actor,
                source_kind='purchase_request',
                source_ref=str(purchase_request.pk),
                notes=f'PO delivery: {purchase_request.title}'[:300],
            )
        except Exception:
            logger.exception(
                'Failed to auto-receive line for PR %s: %s',
                purchase_request.pk, ln,
            )
