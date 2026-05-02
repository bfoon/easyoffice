"""
apps/inventory/models.py
────────────────────────
Inventory, asset and stock-tracking models for EasyOffice.

Two worlds, one app:

  ┌──────────────────────────┐    ┌──────────────────────────┐
  │   PRODUCTS (for sale)    │    │   ASSETS (used by us)    │
  │   "things on the shelf"  │    │   "things on our desks"  │
  ├──────────────────────────┤    ├──────────────────────────┤
  │ • Product (catalog)      │    │ • Asset (single object)  │
  │ • StockItem (per         │    │ • AssetAssignment        │
  │   product per location,  │    │   (who has what, when)   │
  │   quantity-tracked)      │    │ • Maintenance log        │
  │ • StockMovement (in/out) │    │ • Audit log              │
  │ • StockBatch (lot/expiry)│    │                          │
  └──────────────────────────┘    └──────────────────────────┘

Everything that can be physically scanned (Product variants, individual
StockItems, Assets, Locations) carries a stable QR token so a phone scanner
can drive operations from the floor.

State machines are kept in `services.py`. Models hold data + small
denormalised helpers (computed properties, counters) only.
"""
from __future__ import annotations

import secrets
import uuid
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import F, Sum
from django.urls import reverse
from django.utils import timezone


User = settings.AUTH_USER_MODEL


# ════════════════════════════════════════════════════════════════════════════
# Helpers
# ════════════════════════════════════════════════════════════════════════════

def _qr_token() -> str:
    """
    Stable, URL-safe, sufficiently-random token printed onto labels.

    Format: 'eo-<22 chars>'. The 'eo-' prefix lets the scanner reject
    QR codes that aren't ours (e.g. someone scanning a random package).
    """
    return f'eo-{secrets.token_urlsafe(16)}'


# ════════════════════════════════════════════════════════════════════════════
# Locations & Categories
# ════════════════════════════════════════════════════════════════════════════

class Location(models.Model):
    """
    Where stock and assets physically live. A location can be:
      • A shop (sells to customers)
      • A warehouse (storage only)
      • An office floor / room (where assets are deployed)
      • A virtual location (in-transit, customer-site, scrap)

    A scanned QR sticker on a doorframe answers the question
    "which location are we currently moving stock in/out of?"
    """

    class Kind(models.TextChoices):
        SHOP      = 'shop',      'Shop / Retail'
        WAREHOUSE = 'warehouse', 'Warehouse'
        OFFICE    = 'office',    'Office / Workspace'
        VEHICLE   = 'vehicle',   'Vehicle / Van'
        TRANSIT   = 'transit',   'In-Transit'
        SCRAP     = 'scrap',     'Scrap / Disposed'
        OTHER     = 'other',     'Other'

    id        = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    code      = models.CharField(
        max_length=20, unique=True, db_index=True,
        help_text='Short code, e.g. "MAIN-SHOP" or "WH-01".',
    )
    name      = models.CharField(max_length=120)
    kind      = models.CharField(max_length=12, choices=Kind.choices, default=Kind.WAREHOUSE)
    address   = models.CharField(max_length=300, blank=True)
    is_sellable = models.BooleanField(
        default=False,
        help_text='Tick if items here can be sold directly to customers.',
    )
    is_active = models.BooleanField(default=True)

    # QR
    qr_token  = models.CharField(max_length=40, unique=True, default=_qr_token, editable=False)

    # Optional org link — if your project has a Department model
    department = models.ForeignKey(
        'organization.Department', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='inventory_locations',
    )

    notes      = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['kind', 'name']
        indexes = [
            models.Index(fields=['kind', 'is_active']),
        ]

    def __str__(self):
        return f'{self.code} — {self.name}'

    def get_absolute_url(self):
        return reverse('inventory:location_detail', args=[self.pk])


class Category(models.Model):
    """
    Two-level (parent → child) classification used for both products and
    assets. Two levels keeps reports readable without taxonomy overload.
    """
    id        = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name      = models.CharField(max_length=100)
    parent    = models.ForeignKey(
        'self', on_delete=models.CASCADE, null=True, blank=True,
        related_name='children',
    )
    icon      = models.CharField(
        max_length=40, blank=True,
        help_text='Bootstrap-icons class, e.g. "bi-laptop".',
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ['parent__name', 'name']
        verbose_name_plural = 'Categories'
        constraints = [
            models.UniqueConstraint(fields=['parent', 'name'], name='inv_category_unique_under_parent'),
        ]

    def __str__(self):
        return f'{self.parent.name} → {self.name}' if self.parent_id else self.name


class Supplier(models.Model):
    """Vendors we buy from. Keeps purchase history and contact info together."""
    id           = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name         = models.CharField(max_length=180)
    contact_name = models.CharField(max_length=120, blank=True)
    phone        = models.CharField(max_length=30, blank=True)
    email        = models.EmailField(blank=True)
    address      = models.CharField(max_length=300, blank=True)
    tax_id       = models.CharField(max_length=40, blank=True)
    notes        = models.TextField(blank=True)
    is_active    = models.BooleanField(default=True)
    created_at   = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name


# ════════════════════════════════════════════════════════════════════════════
# Products & Stock
# ════════════════════════════════════════════════════════════════════════════

class Product(models.Model):
    """
    A *type* of item — "Solar Panel 250W" — not an individual unit.
    Quantity lives on StockItem, batched by location.

    SKU is required and unique. The QR token printed on shelf-edge labels
    points at the Product (so any unit of this type can be scanned).
    """

    class Kind(models.TextChoices):
        STOCKED  = 'stocked',  'Stocked Product'    # has on-hand quantity
        SERVICE  = 'service',  'Service'            # no stock (e.g. installation)
        BUNDLE   = 'bundle',   'Bundle / Kit'       # composed of other products

    id            = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    sku           = models.CharField(max_length=40, unique=True, db_index=True)
    barcode       = models.CharField(
        max_length=40, blank=True, db_index=True,
        help_text='Manufacturer barcode (EAN/UPC) — leave blank if none.',
    )
    name          = models.CharField(max_length=200)
    description   = models.TextField(blank=True)
    kind          = models.CharField(max_length=10, choices=Kind.choices, default=Kind.STOCKED)
    category      = models.ForeignKey(
        Category, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='products',
    )
    image         = models.ImageField(upload_to='inventory/products/', null=True, blank=True)

    # Pricing
    unit_label    = models.CharField(
        max_length=20, default='unit',
        help_text='How a unit is described, e.g. "piece", "metre", "kg".',
    )
    cost_price    = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))
    sell_price    = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))
    currency      = models.CharField(max_length=3, default='GMD')

    # Stock policy
    is_sellable     = models.BooleanField(default=True)
    is_purchasable  = models.BooleanField(default=True)
    is_for_internal = models.BooleanField(
        default=False,
        help_text='Internal-use consumables (printer paper, cleaning supplies, etc.).',
    )
    track_serial    = models.BooleanField(
        default=False,
        help_text='If enabled, every individual unit must have a unique serial number.',
    )
    track_batch     = models.BooleanField(
        default=False,
        help_text='If enabled, units are grouped into batches with optional expiry.',
    )
    reorder_point   = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal('0.00'),
        help_text='Trigger a low-stock alert when total on-hand falls below this.',
    )
    reorder_qty     = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal('0.00'),
        help_text='Suggested quantity to re-order when low.',
    )

    # Default vendor (for quick PO generation)
    preferred_supplier = models.ForeignKey(
        Supplier, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='preferred_for',
    )

    # QR
    qr_token = models.CharField(max_length=40, unique=True, default=_qr_token, editable=False)

    # Audit
    is_active   = models.BooleanField(default=True)
    created_by  = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='created_products',
    )
    created_at  = models.DateTimeField(auto_now_add=True)
    updated_at  = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['name']
        indexes = [
            models.Index(fields=['kind', 'is_active']),
            models.Index(fields=['is_sellable', 'is_active']),
        ]

    def __str__(self):
        return f'{self.sku} — {self.name}'

    def get_absolute_url(self):
        return reverse('inventory:product_detail', args=[self.pk])

    # ── Aggregated quantities (computed across all locations) ──────────────

    @property
    def total_on_hand(self) -> Decimal:
        if self.kind == self.Kind.SERVICE:
            return Decimal('0.00')
        agg = self.stock_items.aggregate(s=Sum('quantity'))['s']
        return agg or Decimal('0.00')

    @property
    def total_reserved(self) -> Decimal:
        agg = self.stock_items.aggregate(s=Sum('reserved_quantity'))['s']
        return agg or Decimal('0.00')

    @property
    def total_available(self) -> Decimal:
        return self.total_on_hand - self.total_reserved

    @property
    def is_low_stock(self) -> bool:
        if self.kind != self.Kind.STOCKED:
            return False
        return self.total_available < (self.reorder_point or Decimal('0.00'))

    @property
    def stock_status(self) -> str:
        if self.kind != self.Kind.STOCKED:
            return 'na'
        on_hand = self.total_available
        if on_hand <= 0:
            return 'out'
        if on_hand < (self.reorder_point or Decimal('0.00')):
            return 'low'
        return 'ok'


class StockItem(models.Model):
    """
    The (Product × Location) tuple — the actual quantity lives here.

    Reserved quantity is what we've promised but not yet shipped (e.g. an
    order is in PROFORMA_PENDING_SIGNATURE state). Available = on-hand − reserved.
    """
    id        = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    product   = models.ForeignKey(Product, on_delete=models.CASCADE, related_name='stock_items')
    location  = models.ForeignKey(Location, on_delete=models.CASCADE, related_name='stock_items')

    quantity          = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'))
    reserved_quantity = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'))

    # Per-location overrides (optional)
    bin_code  = models.CharField(max_length=30, blank=True, help_text='Aisle/bin/shelf hint.')

    # QR for THIS specific stockitem (so a label on a particular bin/shelf
    # in a particular location resolves directly to the location-specific
    # row, not just to the product).
    qr_token  = models.CharField(max_length=40, unique=True, default=_qr_token, editable=False)

    last_movement_at = models.DateTimeField(null=True, blank=True)
    created_at       = models.DateTimeField(auto_now_add=True)
    updated_at       = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['product__name', 'location__name']
        constraints = [
            models.UniqueConstraint(fields=['product', 'location'], name='inv_stock_unique_per_loc'),
        ]
        indexes = [
            models.Index(fields=['location', 'product']),
        ]

    def __str__(self):
        return f'{self.product.sku} @ {self.location.code}: {self.quantity}'

    @property
    def available(self) -> Decimal:
        return (self.quantity or Decimal('0')) - (self.reserved_quantity or Decimal('0'))


class StockBatch(models.Model):
    """
    Optional batch / lot tracking — for products with expiry dates or where
    you need to know "which shipment did this unit come from".

    Used when Product.track_batch == True. Lives under a StockItem, not a
    Product directly, so each location can have multiple batches.
    """
    id           = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    stock_item   = models.ForeignKey(StockItem, on_delete=models.CASCADE, related_name='batches')
    batch_no     = models.CharField(max_length=50)
    quantity     = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'))
    received_at  = models.DateField(default=timezone.now)
    expiry_date  = models.DateField(null=True, blank=True)
    cost_price   = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    supplier     = models.ForeignKey(
        Supplier, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='batches_supplied',
    )
    notes        = models.TextField(blank=True)

    class Meta:
        ordering = ['expiry_date', 'received_at']
        constraints = [
            models.UniqueConstraint(
                fields=['stock_item', 'batch_no'],
                name='inv_batch_unique_per_stockitem',
            ),
        ]

    def __str__(self):
        return f'{self.batch_no} ({self.stock_item.product.sku})'

    @property
    def is_expired(self) -> bool:
        return bool(self.expiry_date and self.expiry_date < timezone.now().date())

    @property
    def is_expiring_soon(self, days: int = 30) -> bool:
        if not self.expiry_date:
            return False
        delta = self.expiry_date - timezone.now().date()
        return 0 <= delta.days <= days


class StockMovement(models.Model):
    """
    Append-only ledger of every quantity change. Never edit this — issue
    a reverse movement instead. This is the audit backbone.

    A movement is *one-sided* most of the time:
        IN     : positive delta on (product, to_location)
        OUT    : negative delta on (product, from_location)
        ADJUST : either direction on one location

    A TRANSFER is two movements (one OUT, one IN) linked by transfer_group.
    """

    class Kind(models.TextChoices):
        RECEIVE      = 'receive',      'Stock Received'         # purchase / restock
        SALE         = 'sale',         'Sold to Customer'       # order fulfillment
        INTERNAL_USE = 'internal_use', 'Internal Use'           # office consumed
        TRANSFER_IN  = 'transfer_in',  'Transfer In'
        TRANSFER_OUT = 'transfer_out', 'Transfer Out'
        RETURN_IN    = 'return_in',    'Customer Return'
        RETURN_OUT   = 'return_out',   'Returned to Supplier'
        ADJUST_IN    = 'adjust_in',    'Adjustment (Add)'       # stock-take found extra
        ADJUST_OUT   = 'adjust_out',   'Adjustment (Remove)'    # damage / shrinkage / loss
        SCRAP        = 'scrap',        'Scrapped'

    SIGN = {
        Kind.RECEIVE:      +1,
        Kind.SALE:         -1,
        Kind.INTERNAL_USE: -1,
        Kind.TRANSFER_IN:  +1,
        Kind.TRANSFER_OUT: -1,
        Kind.RETURN_IN:    +1,
        Kind.RETURN_OUT:   -1,
        Kind.ADJUST_IN:    +1,
        Kind.ADJUST_OUT:   -1,
        Kind.SCRAP:        -1,
    }

    id          = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    product     = models.ForeignKey(Product, on_delete=models.PROTECT, related_name='movements')
    location    = models.ForeignKey(Location, on_delete=models.PROTECT, related_name='movements')
    batch       = models.ForeignKey(
        StockBatch, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='movements',
    )
    kind        = models.CharField(max_length=15, choices=Kind.choices)
    quantity    = models.DecimalField(
        max_digits=14, decimal_places=2,
        help_text='Always positive — the SIGN constant decides the direction.',
    )

    # Cross-references to whichever document caused this movement.
    # We keep these as soft string references rather than hard FKs so that
    # the inventory app can compile even when other apps are still loading.
    source_kind = models.CharField(
        max_length=30, blank=True,
        help_text='e.g. "sales_order", "purchase_request", "transfer", "stock_take".',
    )
    source_ref  = models.CharField(max_length=80, blank=True, db_index=True)

    transfer_group = models.UUIDField(
        null=True, blank=True, db_index=True,
        help_text='Two movements with the same UUID form a transfer pair.',
    )

    actor       = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='inventory_movements',
    )
    notes       = models.CharField(max_length=300, blank=True)
    occurred_at = models.DateTimeField(default=timezone.now)
    created_at  = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-occurred_at']
        indexes = [
            models.Index(fields=['product', '-occurred_at']),
            models.Index(fields=['location', '-occurred_at']),
            models.Index(fields=['kind', '-occurred_at']),
            models.Index(fields=['source_kind', 'source_ref']),
        ]

    def __str__(self):
        sign = self.SIGN.get(self.kind, 0)
        prefix = '+' if sign > 0 else '-' if sign < 0 else ''
        return f'{prefix}{self.quantity} {self.product.sku} @ {self.location.code} ({self.get_kind_display()})'

    @property
    def signed_quantity(self) -> Decimal:
        return self.quantity * self.SIGN.get(self.kind, 0)

    def clean(self):
        if self.quantity is None or self.quantity <= 0:
            raise ValidationError({'quantity': 'Movement quantity must be positive.'})


# ════════════════════════════════════════════════════════════════════════════
# Stock-take (cycle counts)
# ════════════════════════════════════════════════════════════════════════════

class StockTake(models.Model):
    """
    A scheduled or ad-hoc count of physical stock at a location. We compare
    expected vs counted and emit ADJUST_IN / ADJUST_OUT movements for each
    discrepancy at finalisation.
    """

    class Status(models.TextChoices):
        DRAFT      = 'draft',      'Draft'
        IN_PROGRESS = 'in_progress', 'In Progress'
        COMPLETED  = 'completed',  'Completed'
        CANCELLED  = 'cancelled',  'Cancelled'

    id         = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    reference  = models.CharField(max_length=30, unique=True, editable=False)
    location   = models.ForeignKey(Location, on_delete=models.PROTECT, related_name='stock_takes')
    status     = models.CharField(max_length=15, choices=Status.choices, default=Status.DRAFT)
    started_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='started_stock_takes',
    )
    finalised_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='finalised_stock_takes',
    )
    notes      = models.TextField(blank=True)
    started_at  = models.DateTimeField(default=timezone.now)
    finalised_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-started_at']

    def __str__(self):
        return f'{self.reference} — {self.location.code}'

    def save(self, *args, **kwargs):
        if not self.reference:
            self.reference = f'CT-{timezone.now():%Y%m%d}-{uuid.uuid4().hex[:5].upper()}'
        super().save(*args, **kwargs)


class StockTakeLine(models.Model):
    """One product counted in a stock-take."""
    id          = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    stock_take  = models.ForeignKey(StockTake, on_delete=models.CASCADE, related_name='lines')
    product     = models.ForeignKey(Product, on_delete=models.PROTECT, related_name='stock_take_lines')
    expected    = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'))
    counted     = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    note        = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ['product__name']
        constraints = [
            models.UniqueConstraint(
                fields=['stock_take', 'product'],
                name='inv_stocktake_line_unique_per_product',
            ),
        ]

    @property
    def variance(self) -> Decimal:
        if self.counted is None:
            return Decimal('0.00')
        return self.counted - self.expected


# ════════════════════════════════════════════════════════════════════════════
# Assets (office property)
# ════════════════════════════════════════════════════════════════════════════

class Asset(models.Model):
    """
    A *single* trackable thing the company owns and uses internally.
    Laptop XYZ12345, Office chair #3, Generator, Vehicle, etc.

    Distinct from Product: an asset is one specific unit, with serial,
    purchase date, depreciation, and current custodian.
    """

    class Status(models.TextChoices):
        IN_STORE      = 'in_store',     'In Storage'
        ASSIGNED      = 'assigned',     'Assigned'
        UNDER_REPAIR  = 'under_repair', 'Under Repair'
        RETIRED       = 'retired',      'Retired'
        LOST          = 'lost',         'Lost / Stolen'
        DISPOSED      = 'disposed',     'Disposed'

    class Condition(models.TextChoices):
        NEW       = 'new',       'New'
        GOOD      = 'good',      'Good'
        FAIR      = 'fair',      'Fair'
        POOR      = 'poor',      'Poor'
        BROKEN    = 'broken',    'Broken'

    id              = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tag             = models.CharField(
        max_length=30, unique=True, db_index=True,
        help_text='Internal asset tag (e.g. "EO-LAP-0042"). Auto-generated if blank.',
    )
    name            = models.CharField(max_length=200)
    description     = models.TextField(blank=True)
    category        = models.ForeignKey(
        Category, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='assets',
    )
    serial_number   = models.CharField(max_length=80, blank=True, db_index=True)
    model_number    = models.CharField(max_length=80, blank=True)
    manufacturer    = models.CharField(max_length=120, blank=True)

    # Where it is now
    location        = models.ForeignKey(
        Location, on_delete=models.PROTECT, related_name='assets',
    )
    assigned_to     = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='assigned_assets',
    )
    department      = models.ForeignKey(
        'organization.Department', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='owned_assets',
    )

    # Status & condition
    status          = models.CharField(max_length=15, choices=Status.choices, default=Status.IN_STORE)
    condition       = models.CharField(max_length=10, choices=Condition.choices, default=Condition.GOOD)

    # Money & dates
    purchase_date   = models.DateField(null=True, blank=True)
    purchase_cost   = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'))
    currency        = models.CharField(max_length=3, default='GMD')
    supplier        = models.ForeignKey(
        Supplier, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='supplied_assets',
    )
    warranty_until  = models.DateField(null=True, blank=True)

    # Depreciation (straight-line, simple)
    useful_life_months = models.PositiveSmallIntegerField(
        default=0,
        help_text='Months over which to straight-line depreciate. 0 = no depreciation.',
    )
    salvage_value      = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))

    # Optional link if this asset was created from a sourced product
    source_product = models.ForeignKey(
        Product, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='assets_created_from',
    )

    image           = models.ImageField(upload_to='inventory/assets/', null=True, blank=True)
    qr_token        = models.CharField(max_length=40, unique=True, default=_qr_token, editable=False)

    notes           = models.TextField(blank=True)
    is_active       = models.BooleanField(default=True)
    created_by      = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='created_assets',
    )
    created_at      = models.DateTimeField(auto_now_add=True)
    updated_at      = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['tag']
        indexes = [
            models.Index(fields=['status', '-updated_at']),
            models.Index(fields=['assigned_to', 'status']),
        ]

    def __str__(self):
        return f'{self.tag} — {self.name}'

    def get_absolute_url(self):
        return reverse('inventory:asset_detail', args=[self.pk])

    def save(self, *args, **kwargs):
        if not self.tag:
            self.tag = f'EO-AST-{uuid.uuid4().hex[:6].upper()}'
        super().save(*args, **kwargs)

    # ── Depreciation helpers ───────────────────────────────────────────────

    @property
    def book_value(self) -> Decimal:
        """
        Straight-line book value as of today. Returns purchase_cost when
        no useful_life_months is set.
        """
        if not self.purchase_date or not self.useful_life_months:
            return self.purchase_cost
        elapsed_months = (
            (timezone.now().date().year  - self.purchase_date.year ) * 12 +
            (timezone.now().date().month - self.purchase_date.month)
        )
        elapsed_months = max(0, min(elapsed_months, self.useful_life_months))
        depreciable = max(self.purchase_cost - self.salvage_value, Decimal('0'))
        per_month = depreciable / Decimal(self.useful_life_months) if self.useful_life_months else Decimal('0')
        depreciated = per_month * Decimal(elapsed_months)
        return max(self.purchase_cost - depreciated, self.salvage_value)

    @property
    def is_under_warranty(self) -> bool:
        return bool(self.warranty_until and self.warranty_until >= timezone.now().date())

    @property
    def status_color(self) -> str:
        return {
            'in_store':     '#64748b',
            'assigned':     '#10b981',
            'under_repair': '#f59e0b',
            'retired':      '#94a3b8',
            'lost':         '#ef4444',
            'disposed':     '#475569',
        }.get(self.status, '#64748b')


class AssetAssignment(models.Model):
    """
    Append-only history of who held an asset and when. Closing an
    assignment (returned_at != null) implicitly opens the asset back up
    for re-assignment.
    """
    id          = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    asset       = models.ForeignKey(Asset, on_delete=models.CASCADE, related_name='assignments')
    assignee    = models.ForeignKey(
        User, on_delete=models.PROTECT, related_name='asset_assignments',
    )
    assigned_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='asset_assignments_made',
    )

    assigned_at  = models.DateTimeField(default=timezone.now)
    returned_at  = models.DateTimeField(null=True, blank=True)
    return_condition = models.CharField(
        max_length=10, choices=Asset.Condition.choices, blank=True,
    )
    purpose      = models.CharField(max_length=300, blank=True)
    return_note  = models.TextField(blank=True)

    class Meta:
        ordering = ['-assigned_at']
        indexes = [
            models.Index(fields=['asset', '-assigned_at']),
            models.Index(fields=['assignee', '-assigned_at']),
        ]

    def __str__(self):
        suffix = '' if self.returned_at else ' (active)'
        return f'{self.asset.tag} → {self.assignee}{suffix}'

    @property
    def is_active(self) -> bool:
        return self.returned_at is None


class AssetMaintenance(models.Model):
    """Repairs, services, calibrations, inspections."""

    class Kind(models.TextChoices):
        REPAIR     = 'repair',     'Repair'
        SERVICE    = 'service',    'Routine Service'
        INSPECTION = 'inspection', 'Inspection'
        UPGRADE    = 'upgrade',    'Upgrade'
        OTHER      = 'other',      'Other'

    id           = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    asset        = models.ForeignKey(Asset, on_delete=models.CASCADE, related_name='maintenance_records')
    kind         = models.CharField(max_length=12, choices=Kind.choices, default=Kind.REPAIR)
    description  = models.TextField()
    cost         = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))
    performed_by = models.CharField(
        max_length=200, blank=True,
        help_text='External vendor or internal staff name.',
    )
    performed_on = models.DateField(default=timezone.now)
    next_due     = models.DateField(null=True, blank=True)
    recorded_by  = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='maintenance_records_logged',
    )
    attachment   = models.FileField(upload_to='inventory/maintenance/', null=True, blank=True)
    created_at   = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-performed_on']

    def __str__(self):
        return f'{self.asset.tag} — {self.get_kind_display()} ({self.performed_on})'


class AssetEvent(models.Model):
    """Append-only audit trail for assets — mirrors OrderEvent in shape."""

    class Kind(models.TextChoices):
        CREATED     = 'created',     'Created'
        UPDATED     = 'updated',     'Updated'
        ASSIGNED    = 'assigned',    'Assigned'
        RETURNED    = 'returned',    'Returned'
        TRANSFERRED = 'transferred', 'Location Changed'
        STATUS      = 'status',      'Status Changed'
        MAINTENANCE = 'maintenance', 'Maintenance Logged'
        SCRAPPED    = 'scrapped',    'Scrapped / Disposed'
        NOTE        = 'note',        'Note'

    id        = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    asset     = models.ForeignKey(Asset, on_delete=models.CASCADE, related_name='events')
    kind      = models.CharField(max_length=15, choices=Kind.choices)
    actor     = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='asset_events',
    )
    message   = models.CharField(max_length=400)
    payload   = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.asset.tag}: {self.get_kind_display()}'


# ════════════════════════════════════════════════════════════════════════════
# Internal Stock Requests (staff requesting items from the shop / store)
# ════════════════════════════════════════════════════════════════════════════

class StockRequest(models.Model):
    """
    Lightweight internal request — "I need 5 packs of A4 paper from the
    main store." If we have it, the storekeeper issues it (creates an
    INTERNAL_USE movement). If we don't, we route a Purchase Request
    in the finance app.
    """

    class Status(models.TextChoices):
        PENDING   = 'pending',   'Pending'
        APPROVED  = 'approved',  'Approved'
        ISSUED    = 'issued',    'Issued'
        PARTIAL   = 'partial',   'Partially Issued'
        REROUTED  = 'rerouted',  'Rerouted to Purchase'
        REJECTED  = 'rejected',  'Rejected'
        CANCELLED = 'cancelled', 'Cancelled'

    id           = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    reference    = models.CharField(max_length=30, unique=True, editable=False)
    requested_by = models.ForeignKey(
        User, on_delete=models.PROTECT, related_name='stock_requests',
    )
    department   = models.ForeignKey(
        'organization.Department', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='stock_requests',
    )
    location     = models.ForeignKey(
        Location, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='outgoing_stock_requests',
        help_text='Source location preference. Optional — service.py picks one if blank.',
    )
    purpose      = models.CharField(max_length=300, blank=True)
    status       = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING)

    issued_by    = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='issued_stock_requests',
    )
    issued_at    = models.DateTimeField(null=True, blank=True)

    # If we couldn't fulfil from stock and rerouted to a finance purchase
    rerouted_to_purchase = models.UUIDField(null=True, blank=True)
    rerouted_at = models.DateTimeField(null=True, blank=True)

    notes        = models.TextField(blank=True)
    created_at   = models.DateTimeField(auto_now_add=True)
    updated_at   = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def save(self, *args, **kwargs):
        if not self.reference:
            self.reference = f'SR-{timezone.now():%Y%m%d}-{uuid.uuid4().hex[:5].upper()}'
        super().save(*args, **kwargs)

    def __str__(self):
        return self.reference


class StockRequestLine(models.Model):
    id            = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    request       = models.ForeignKey(StockRequest, on_delete=models.CASCADE, related_name='lines')
    product       = models.ForeignKey(Product, on_delete=models.PROTECT, related_name='+')
    quantity      = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('1.00'))
    issued_qty    = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))
    note          = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ['id']

    @property
    def is_fulfilled(self):
        return self.issued_qty >= self.quantity

    def __str__(self):
        return f'{self.product.sku} ×{self.quantity}'


# ════════════════════════════════════════════════════════════════════════════
# Access control
# ════════════════════════════════════════════════════════════════════════════

class InventoryAccessGrant(models.Model):
    """
    Grants access to a single inventory module at a given level, scoped to
    either an individual user OR a department. Exactly one of `user` /
    `department` is set per row.

    The effective access for a user on a module is the *highest* level
    across every grant that touches them — see apps.inventory.access.

    Grants can be time-bound via `expires_at` (e.g. give a contractor
    operate-on-products until 2026-12-31).
    """
    from .access import Module, Level   # local import to avoid stale strings

    id            = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    user          = models.ForeignKey(
        User, on_delete=models.CASCADE,
        null=True, blank=True, related_name='inventory_access_grants',
    )
    department    = models.ForeignKey(
        'organization.Department', on_delete=models.CASCADE,
        null=True, blank=True, related_name='inventory_access_grants',
    )

    module        = models.CharField(max_length=20, choices=Module.CHOICES)
    level         = models.CharField(max_length=10, choices=Level.CHOICES, default='view')

    is_active     = models.BooleanField(default=True)
    expires_at    = models.DateTimeField(null=True, blank=True)
    notes         = models.CharField(max_length=200, blank=True)

    granted_by    = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='+',
    )
    granted_at    = models.DateTimeField(auto_now_add=True)
    updated_at    = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Inventory access grant'
        ordering = ['-granted_at']
        indexes = [
            models.Index(fields=['user', 'is_active', 'module']),
            models.Index(fields=['department', 'is_active', 'module']),
        ]
        constraints = [
            # Exactly one of user / department must be set.
            models.CheckConstraint(
                name='inv_grant_user_xor_department',
                check=(
                    models.Q(user__isnull=False, department__isnull=True) |
                    models.Q(user__isnull=True,  department__isnull=False)
                ),
            ),
            # Don't allow duplicate active grants (same scope+module).
            models.UniqueConstraint(
                fields=['user', 'module'], name='inv_grant_unique_user_module',
                condition=models.Q(user__isnull=False),
            ),
            models.UniqueConstraint(
                fields=['department', 'module'],
                name='inv_grant_unique_department_module',
                condition=models.Q(department__isnull=False),
            ),
        ]

    def __str__(self):
        scope = self.user or self.department
        return f'{scope} → {self.module} ({self.level})'

    @property
    def scope_label(self):
        if self.user_id:
            return f'User: {self.user.get_full_name() or self.user.username}'
        if self.department_id:
            return f'Department: {self.department.name}'
        return 'Unknown scope'
