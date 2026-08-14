"""
apps/inventory/license_models.py
────────────────────────────────
Software / service **licence** management for EasyOffice.

Why a separate file: `models.py` is already large, and licences are a
self-contained world. Django picks these up because `models.py` imports
this module at the bottom (one line — see the install notes in
LICENSES_README.md). All cross-model references use the string form
('inventory.Supplier') so there is no import cycle.

Three things are tracked
════════════════════════

  ┌────────────────────────────────────────────────────────────────┐
  │ LicenseType  — the catalogue entry: "Microsoft 365 Business    │
  │                Standard", vendor, default term, list cost      │
  │                and list selling price.                         │
  ├────────────────────────────────────────────────────────────────┤
  │ License      — one purchased contract. Ours (internal), bought │
  │                for a named staff member, or bought on behalf   │
  │                of a customer company. Carries seats, cost per  │
  │                seat, selling price, start/end dates and the    │
  │                reminder schedule.                              │
  ├────────────────────────────────────────────────────────────────┤
  │ LicenseSeat  — who is actually consuming a seat. This is what  │
  │                turns "cost per licence" into "cost per user".  │
  └────────────────────────────────────────────────────────────────┘

Plus `LicenseRenewal` (an append-only record of every term extension)
and `LicenseEvent` (the audit trail, same shape as AssetEvent).

Money notes
───────────
`unit_cost` / `unit_price` are per **seat** for per-user and per-device
licences, and per **licence** for flat/site licences. Everything else —
total cost, cost per user, margin, monthly and annualised run-rate — is
derived, never stored, so a corrected seat count instantly corrects the
numbers everywhere.
"""
from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.urls import reverse
from django.utils import timezone


User = settings.AUTH_USER_MODEL

TWOPLACES = Decimal('0.01')

#: Default reminder ladder, in days before expiry. Override per licence on
#: the record itself, or globally with ``INVENTORY_LICENSE_REMINDER_DAYS``.
DEFAULT_REMINDER_DAYS = [90, 60, 30, 14, 7, 3, 1]

#: A licence is shown as "expiring soon" inside this window.
DEFAULT_WARN_DAYS = 30


def _money(value) -> Decimal:
    """Round to 2dp, half-up — never trust float drift into a report."""
    if value is None:
        return Decimal('0.00')
    return Decimal(value).quantize(TWOPLACES, rounding=ROUND_HALF_UP)


def warn_days() -> int:
    return int(getattr(settings, 'INVENTORY_LICENSE_WARN_DAYS', DEFAULT_WARN_DAYS))


def default_reminder_days() -> list[int]:
    raw = getattr(settings, 'INVENTORY_LICENSE_REMINDER_DAYS', DEFAULT_REMINDER_DAYS)
    try:
        return sorted({int(x) for x in raw}, reverse=True)
    except Exception:
        return list(DEFAULT_REMINDER_DAYS)


def _default_reminder_string() -> str:
    return ','.join(str(d) for d in default_reminder_days())


# ════════════════════════════════════════════════════════════════════════════
# Shared choice sets
# ════════════════════════════════════════════════════════════════════════════

class BillingModel(models.TextChoices):
    PER_USER    = 'per_user',    'Per user / seat'
    PER_DEVICE  = 'per_device',  'Per device'
    PER_LICENSE = 'per_license', 'Flat — per licence'
    SITE        = 'site',        'Site / unlimited'
    USAGE       = 'usage',       'Usage / metered'


#: Billing models where cost and price scale with the seat count.
PER_SEAT_MODELS = {BillingModel.PER_USER, BillingModel.PER_DEVICE}


class BillingCycle(models.TextChoices):
    MONTHLY   = 'monthly',   'Monthly'
    QUARTERLY = 'quarterly', 'Quarterly'
    ANNUAL    = 'annual',    'Annual'
    BIENNIAL  = 'biennial',  'Every 2 years'
    ONE_OFF   = 'one_off',   'One-off / perpetual'


# ════════════════════════════════════════════════════════════════════════════
# Catalogue
# ════════════════════════════════════════════════════════════════════════════

class LicenseType(models.Model):
    """
    A licensable product we buy repeatedly. Creating a licence from a type
    pre-fills the term, the cost and the selling price, so the storekeeper
    doesn't retype the commercials every renewal.
    """
    id            = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    code          = models.CharField(
        max_length=40, unique=True, db_index=True,
        help_text='Short code, e.g. "M365-BUS-STD".',
    )
    name          = models.CharField(max_length=200)
    description   = models.TextField(blank=True)
    vendor        = models.ForeignKey(
        'inventory.Supplier', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='license_types',
        help_text='Who we buy it from — reuses the inventory supplier list.',
    )
    category      = models.ForeignKey(
        'inventory.Category', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='license_types',
    )
    product       = models.ForeignKey(
        'inventory.Product', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='license_types',
        help_text='Optional link to the catalogue product, if we also sell it.',
    )

    billing_model = models.CharField(
        max_length=12, choices=BillingModel.choices, default=BillingModel.PER_USER,
    )
    billing_cycle = models.CharField(
        max_length=10, choices=BillingCycle.choices, default=BillingCycle.ANNUAL,
    )
    default_term_months = models.PositiveSmallIntegerField(
        default=12, help_text='Standard contract length. 0 = perpetual.',
    )
    default_unit_cost  = models.DecimalField(
        max_digits=14, decimal_places=2, default=Decimal('0.00'),
        help_text='What we pay, per seat (or per licence for flat models).',
    )
    default_unit_price = models.DecimalField(
        max_digits=14, decimal_places=2, default=Decimal('0.00'),
        help_text='What we charge a customer. Leave 0 for internal-only licences.',
    )
    currency      = models.CharField(max_length=3, default='GMD')

    support_url   = models.URLField(blank=True)
    notes         = models.TextField(blank=True)
    is_active     = models.BooleanField(default=True)
    created_at    = models.DateTimeField(auto_now_add=True)
    updated_at    = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['name']
        verbose_name = 'Licence type'
        verbose_name_plural = 'Licence types'

    def __str__(self):
        return f'{self.code} — {self.name}'

    def get_absolute_url(self):
        return reverse('inventory:license_type_list')

    @property
    def default_margin(self) -> Decimal:
        return _money(self.default_unit_price - self.default_unit_cost)


# ════════════════════════════════════════════════════════════════════════════
# The licence itself
# ════════════════════════════════════════════════════════════════════════════

class License(models.Model):
    """
    One licence contract with a start, an end, a price and a seat count.

    `holder_kind` answers "who is this for?":
        internal — our own licence, cost only, no selling price
        user     — bought for one named member of staff
        customer — bought on behalf of a client; selling price and margin
                   apply, and the client can be copied on expiry warnings
    """

    class Holder(models.TextChoices):
        INTERNAL = 'internal', 'Ours (internal)'
        USER     = 'user',     'Bought for a staff member'
        CUSTOMER = 'customer', 'Bought for a customer'

    class Status(models.TextChoices):
        DRAFT      = 'draft',      'Draft'
        ACTIVE     = 'active',     'Active'
        SUSPENDED  = 'suspended',  'Suspended'
        EXPIRED    = 'expired',    'Expired'
        CANCELLED  = 'cancelled',  'Cancelled'
        SUPERSEDED = 'superseded', 'Superseded (replaced)'

    #: Statuses where expiry maths and reminders still matter.
    LIVE_STATUSES = ('draft', 'active', 'suspended')

    id            = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    reference     = models.CharField(max_length=30, unique=True, editable=False, db_index=True)
    name          = models.CharField(
        max_length=200,
        help_text='What this licence is, in plain words — shown in lists and alerts.',
    )
    license_type  = models.ForeignKey(
        LicenseType, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='licenses',
    )
    vendor        = models.ForeignKey(
        'inventory.Supplier', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='licenses',
    )

    # ── Who it's for ────────────────────────────────────────────────────────
    holder_kind    = models.CharField(
        max_length=10, choices=Holder.choices, default=Holder.INTERNAL, db_index=True,
    )
    holder_user    = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='licenses_held',
        help_text='Set when the licence belongs to one named staff member.',
    )
    customer_name  = models.CharField(max_length=200, blank=True)
    customer_email = models.EmailField(
        blank=True, help_text='Copied on expiry warnings when "notify customer" is on.',
    )
    customer_ref   = models.CharField(
        max_length=100, blank=True,
        help_text='Soft cross-app reference, e.g. "crm_customer:0f3c…".',
    )
    department     = models.ForeignKey(
        'organization.Department', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='licenses',
    )
    owner          = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='licenses_owned',
        help_text='Internal person accountable for renewing this.',
    )

    # ── Credentials ─────────────────────────────────────────────────────────
    license_key    = models.CharField(
        max_length=255, blank=True,
        help_text='Product key or subscription ID. Masked unless you can manage licences.',
    )
    account_email  = models.EmailField(blank=True, help_text='Account the licence sits under.')
    portal_url     = models.URLField(blank=True)
    attachment     = models.FileField(
        upload_to='inventory/licenses/', null=True, blank=True,
        help_text='Certificate, invoice or agreement PDF.',
    )

    # ── Commercials ─────────────────────────────────────────────────────────
    billing_model  = models.CharField(
        max_length=12, choices=BillingModel.choices, default=BillingModel.PER_USER,
    )
    billing_cycle  = models.CharField(
        max_length=10, choices=BillingCycle.choices, default=BillingCycle.ANNUAL,
    )
    seats          = models.PositiveIntegerField(
        default=1, help_text='How many users / devices this licence covers.',
    )
    unit_cost      = models.DecimalField(
        max_digits=14, decimal_places=2, default=Decimal('0.00'),
        help_text='Cost per seat (per licence for flat models), for the whole term.',
    )
    unit_price     = models.DecimalField(
        max_digits=14, decimal_places=2, default=Decimal('0.00'),
        help_text='Selling price per seat / licence. 0 for internal licences.',
    )
    setup_cost     = models.DecimalField(
        max_digits=14, decimal_places=2, default=Decimal('0.00'),
        help_text='One-off cost on top — onboarding, migration, activation.',
    )
    setup_price    = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'))
    currency       = models.CharField(max_length=3, default='GMD')
    purchase_ref   = models.CharField(
        max_length=100, blank=True,
        help_text='Invoice / PO reference, e.g. "purchase_request:8b1a…".',
    )

    # ── Term ────────────────────────────────────────────────────────────────
    start_date     = models.DateField(default=timezone.localdate)
    end_date       = models.DateField(
        null=True, blank=True, db_index=True,
        help_text='Leave blank only for perpetual licences.',
    )
    is_perpetual   = models.BooleanField(
        default=False, help_text='Never expires — no reminders will be sent.',
    )
    auto_renew     = models.BooleanField(
        default=False, help_text='Vendor renews automatically unless we cancel.',
    )
    renewal_term_months = models.PositiveSmallIntegerField(
        default=12, help_text='Term applied by the Renew action.',
    )
    grace_days     = models.PositiveSmallIntegerField(
        default=0, help_text='Days of service after the end date before it truly stops.',
    )

    # ── Alerts ──────────────────────────────────────────────────────────────
    alerts_enabled  = models.BooleanField(default=True)
    notify_customer = models.BooleanField(
        default=False, help_text='Also email the customer contact on expiry warnings.',
    )
    reminder_days   = models.CharField(
        max_length=100, blank=True, default=_default_reminder_string,
        help_text='Days before expiry to warn, comma separated. Blank uses the default ladder.',
    )
    reminders_sent  = models.JSONField(
        default=list, blank=True, editable=False,
        help_text='Thresholds already fired for the current term.',
    )
    last_reminder_at    = models.DateTimeField(null=True, blank=True, editable=False)
    expired_notified_at = models.DateTimeField(null=True, blank=True, editable=False)

    # ── Meta ────────────────────────────────────────────────────────────────
    status     = models.CharField(
        max_length=12, choices=Status.choices, default=Status.ACTIVE, db_index=True,
    )
    notes      = models.TextField(blank=True)
    is_active  = models.BooleanField(default=True)
    created_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name='licenses_created',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['end_date', 'name']
        verbose_name = 'Licence'
        verbose_name_plural = 'Licences'
        indexes = [
            models.Index(fields=['status', 'end_date']),
            models.Index(fields=['holder_kind', 'status']),
            models.Index(fields=['owner', 'status']),
        ]

    # ── Lifecycle ───────────────────────────────────────────────────────────

    def __str__(self):
        return f'{self.reference} — {self.name}'

    def save(self, *args, **kwargs):
        if not self.reference:
            self.reference = f'LIC-{timezone.now():%Y%m}-{uuid.uuid4().hex[:5].upper()}'
        if self.is_perpetual:
            self.end_date = None
        super().save(*args, **kwargs)

    def clean(self):
        if not self.is_perpetual and not self.end_date:
            raise ValidationError({'end_date': 'Set an end date, or tick "perpetual".'})
        if self.end_date and self.end_date < self.start_date:
            raise ValidationError({'end_date': 'The end date is before the start date.'})
        if self.holder_kind == self.Holder.USER and not self.holder_user_id:
            raise ValidationError({'holder_user': 'Pick the staff member this licence is for.'})
        if self.holder_kind == self.Holder.CUSTOMER and not self.customer_name.strip():
            raise ValidationError({'customer_name': 'Name the customer this licence is for.'})
        if self.notify_customer and not self.customer_email:
            raise ValidationError({'customer_email': 'Add a customer email, or turn off the customer copy.'})

    def get_absolute_url(self):
        return reverse('inventory:license_detail', args=[self.pk])

    # ── Seats ───────────────────────────────────────────────────────────────

    @property
    def is_per_seat(self) -> bool:
        return self.billing_model in PER_SEAT_MODELS

    @property
    def seats_assigned(self) -> int:
        return self.assignments.filter(released_at__isnull=True).count()

    @property
    def seats_free(self) -> int:
        return max(self.seats - self.seats_assigned, 0)

    @property
    def is_overallocated(self) -> bool:
        return self.seats_assigned > self.seats

    @property
    def seat_fill_pct(self) -> int:
        if not self.seats:
            return 0
        return min(int(round(self.seats_assigned * 100 / self.seats)), 100)

    # ── Money ───────────────────────────────────────────────────────────────

    @property
    def recurring_cost(self) -> Decimal:
        """Cost of the licence itself for one term, before one-off fees."""
        if self.is_per_seat:
            return _money(self.unit_cost * self.seats)
        return _money(self.unit_cost)

    @property
    def recurring_price(self) -> Decimal:
        if self.is_per_seat:
            return _money(self.unit_price * self.seats)
        return _money(self.unit_price)

    @property
    def total_cost(self) -> Decimal:
        """Everything we pay for one term."""
        return _money(self.recurring_cost + self.setup_cost)

    @property
    def total_price(self) -> Decimal:
        """Everything we invoice for one term. Zero on internal licences."""
        return _money(self.recurring_price + self.setup_price)

    @property
    def margin(self) -> Decimal:
        return _money(self.total_price - self.total_cost)

    @property
    def margin_pct(self):
        if not self.total_price:
            return None
        return round(float(self.margin) / float(self.total_price) * 100, 1)

    @property
    def cost_per_seat(self) -> Decimal:
        """Cost spread over every seat we paid for."""
        if not self.seats:
            return self.total_cost
        return _money(self.total_cost / Decimal(self.seats))

    @property
    def price_per_seat(self) -> Decimal:
        if not self.seats:
            return self.total_price
        return _money(self.total_price / Decimal(self.seats))

    @property
    def cost_per_assigned_user(self):
        """
        The number people actually ask for: what each *used* seat really
        costs. Empty seats push this above `cost_per_seat` — which is the
        point, it makes waste visible.
        """
        used = self.seats_assigned
        if not used:
            return None
        return _money(self.total_cost / Decimal(used))

    @property
    def wasted_cost(self) -> Decimal:
        """Money sitting in seats nobody is using."""
        if not self.is_per_seat:
            return Decimal('0.00')
        return _money(self.cost_per_seat * Decimal(self.seats_free))

    @property
    def term_days(self) -> int:
        if not self.end_date:
            return 0
        return max((self.end_date - self.start_date).days, 1)

    @property
    def daily_cost(self) -> Decimal:
        if not self.term_days:
            return Decimal('0.00')
        return _money(self.total_cost / Decimal(self.term_days))

    @property
    def annualised_cost(self) -> Decimal:
        """
        Run-rate — lets you add monthly and 3-year contracts together.
        Divided once at the end so a 365-day term annualises to exactly
        its own cost rather than a rounding cent above it.
        """
        if not self.term_days:
            return Decimal('0.00')
        return _money(self.total_cost * Decimal(365) / Decimal(self.term_days))

    @property
    def monthly_cost(self) -> Decimal:
        return _money(self.annualised_cost / Decimal(12))

    @property
    def annualised_price(self) -> Decimal:
        if not self.term_days:
            return Decimal('0.00')
        return _money(self.total_price * Decimal(365) / Decimal(self.term_days))

    @property
    def renewal_estimate(self) -> Decimal:
        """What the next renewal will cost at today's rates."""
        return self.total_cost

    # ── Time ────────────────────────────────────────────────────────────────

    @property
    def expiry_date(self):
        """End date plus any grace period — the real service cut-off."""
        if not self.end_date:
            return None
        return self.end_date + timedelta(days=self.grace_days or 0)

    @property
    def days_remaining(self):
        """Days until the end date. Negative once past. None if perpetual."""
        if self.is_perpetual or not self.end_date:
            return None
        return (self.end_date - timezone.localdate()).days

    @property
    def is_expired(self) -> bool:
        d = self.days_remaining
        return d is not None and d < 0

    @property
    def is_in_grace(self) -> bool:
        if not self.is_expired or not self.grace_days:
            return False
        return timezone.localdate() <= self.expiry_date

    @property
    def is_expiring_soon(self) -> bool:
        d = self.days_remaining
        return d is not None and 0 <= d <= warn_days()

    @property
    def term_progress_pct(self) -> int:
        if not self.end_date:
            return 0
        elapsed = (timezone.localdate() - self.start_date).days
        return max(0, min(int(round(elapsed * 100 / self.term_days)), 100))

    # ── Presentation ────────────────────────────────────────────────────────

    @property
    def health(self) -> str:
        """One word for the state of this licence — drives every colour."""
        if self.status in (self.Status.CANCELLED, self.Status.SUPERSEDED):
            return 'closed'
        if self.status == self.Status.SUSPENDED:
            return 'suspended'
        if self.is_perpetual:
            return 'perpetual'
        d = self.days_remaining
        if d is None:
            return 'unknown'
        if d < 0:
            return 'grace' if self.is_in_grace else 'expired'
        if d <= 7:
            return 'critical'
        if d <= warn_days():
            return 'soon'
        return 'ok'

    @property
    def health_label(self) -> str:
        d = self.days_remaining
        return {
            'closed':    self.get_status_display(),
            'suspended': 'Suspended',
            'perpetual': 'Perpetual',
            'unknown':   'No end date',
            'expired':   f'Expired {abs(d) if d is not None else 0} day(s) ago',
            'grace':     'In grace period',
            'critical':  f'Expires in {d} day(s)',
            'soon':      f'Expires in {d} day(s)',
            'ok':        f'{d} day(s) left',
        }[self.health]

    @property
    def health_color(self) -> str:
        return {
            'ok':        '#047857',
            'soon':      '#b45309',
            'critical':  '#be123c',
            'expired':   '#be123c',
            'grace':     '#c2410c',
            'suspended': '#6d28d9',
            'perpetual': '#1d4ed8',
            'closed':    '#64748b',
            'unknown':   '#64748b',
        }[self.health]

    @property
    def health_chip(self) -> str:
        """Maps onto the inv-chip-* classes already in the base template."""
        return {
            'ok':        'inv-chip-green',
            'soon':      'inv-chip-amber',
            'critical':  'inv-chip-rose',
            'expired':   'inv-chip-rose',
            'grace':     'inv-chip-orange',
            'suspended': 'inv-chip-violet',
            'perpetual': 'inv-chip-blue',
            'closed':    'inv-chip-slate',
            'unknown':   'inv-chip-slate',
        }[self.health]

    @property
    def holder_label(self) -> str:
        if self.holder_kind == self.Holder.CUSTOMER:
            return self.customer_name or 'Customer'
        if self.holder_kind == self.Holder.USER and self.holder_user_id:
            return self.holder_user.get_full_name() or self.holder_user.username
        return 'Internal'

    @property
    def masked_key(self) -> str:
        """Never render the raw key to someone who can only view."""
        key = (self.license_key or '').strip()
        if not key:
            return ''
        if len(key) <= 4:
            return '•' * len(key)
        return f'{"•" * 8}{key[-4:]}'

    # ── Reminder ladder ─────────────────────────────────────────────────────

    def get_reminder_days(self) -> list[int]:
        raw = (self.reminder_days or '').strip()
        if not raw:
            return default_reminder_days()
        out = set()
        for chunk in raw.replace(';', ',').split(','):
            chunk = chunk.strip()
            if chunk.isdigit():
                out.add(int(chunk))
        return sorted(out, reverse=True) or default_reminder_days()

    @property
    def fired_reminders(self) -> set[int]:
        return {int(x) for x in (self.reminders_sent or [])
                if str(x).lstrip('-').isdigit()}

    @property
    def crossed_reminders(self) -> list[int]:
        """
        Thresholds we have passed and not yet warned about.

        A threshold of 30 means "warn once we're inside 30 days". If the
        scan hasn't run for a while, several thresholds can be crossed at
        once — they all get marked as sent so the inbox gets one clear
        warning, not a burst of five.
        """
        d = self.days_remaining
        if d is None or d < 0:
            return []
        fired = self.fired_reminders
        return sorted(t for t in self.get_reminder_days() if d <= t and t not in fired)

    @property
    def next_reminder_day(self):
        """The tightest crossed threshold still waiting to be sent."""
        crossed = self.crossed_reminders
        return crossed[0] if crossed else None


# ════════════════════════════════════════════════════════════════════════════
# Seats — the cost-per-user layer
# ════════════════════════════════════════════════════════════════════════════

class LicenseSeat(models.Model):
    """
    One consumed seat. Either an internal user (FK) or a named external
    person (for customer licences we administer). Append-only in spirit:
    releasing a seat stamps `released_at`, it never deletes the row, so
    "who had this last year" stays answerable.
    """
    id           = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    license      = models.ForeignKey(
        License, on_delete=models.CASCADE, related_name='assignments',
    )
    user         = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='license_seats',
    )
    person_name  = models.CharField(
        max_length=160, blank=True, help_text='For people who have no EasyOffice account.',
    )
    person_email = models.EmailField(blank=True)
    device_label = models.CharField(
        max_length=120, blank=True, help_text='For per-device licences — machine name or asset tag.',
    )
    asset        = models.ForeignKey(
        'inventory.Asset', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='license_seats',
        help_text='Link the seat to the machine it is installed on.',
    )

    cost_override = models.DecimalField(
        max_digits=14, decimal_places=2, null=True, blank=True,
        help_text='Use when this particular seat costs something different.',
    )

    assigned_at   = models.DateTimeField(default=timezone.now)
    assigned_by   = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name='license_seats_given',
    )
    released_at   = models.DateTimeField(null=True, blank=True)
    released_by   = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name='license_seats_released',
    )
    release_reason = models.CharField(max_length=200, blank=True)
    note          = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ['-assigned_at']
        verbose_name = 'Licence seat'
        constraints = [
            models.UniqueConstraint(
                fields=['license', 'user'],
                condition=models.Q(released_at__isnull=True, user__isnull=False),
                name='inv_license_seat_unique_active_user',
            ),
        ]
        indexes = [
            models.Index(fields=['license', 'released_at']),
            models.Index(fields=['user', 'released_at']),
        ]

    def __str__(self):
        return f'{self.license.reference} → {self.holder_label}'

    @property
    def is_active(self) -> bool:
        return self.released_at is None

    @property
    def holder_label(self) -> str:
        if self.user_id:
            return self.user.get_full_name() or self.user.username
        if self.person_name:
            return self.person_name
        if self.device_label:
            return self.device_label
        return 'Unassigned seat'

    @property
    def email(self) -> str:
        return self.person_email or getattr(self.user, 'email', '') or ''

    @property
    def effective_cost(self) -> Decimal:
        if self.cost_override is not None:
            return _money(self.cost_override)
        return self.license.cost_per_seat

    @property
    def days_held(self) -> int:
        end = self.released_at or timezone.now()
        return max((end - self.assigned_at).days, 0)


# ════════════════════════════════════════════════════════════════════════════
# Renewals
# ════════════════════════════════════════════════════════════════════════════

class LicenseRenewal(models.Model):
    """Append-only record of each term extension, with the price we paid."""
    id            = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    license       = models.ForeignKey(
        License, on_delete=models.CASCADE, related_name='renewals',
    )
    previous_end  = models.DateField(null=True, blank=True)
    new_end       = models.DateField()
    term_months   = models.PositiveSmallIntegerField(default=12)

    seats         = models.PositiveIntegerField(default=1)
    unit_cost     = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'))
    unit_price    = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'))
    total_cost    = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'))
    total_price   = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'))
    currency      = models.CharField(max_length=3, default='GMD')

    invoice_ref   = models.CharField(max_length=100, blank=True)
    notes         = models.CharField(max_length=300, blank=True)
    renewed_by    = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name='license_renewals',
    )
    renewed_at    = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-renewed_at']
        verbose_name = 'Licence renewal'

    def __str__(self):
        return f'{self.license.reference} → {self.new_end}'

    @property
    def cost_delta(self) -> Decimal:
        """Change against the previous term — the number finance asks about."""
        prev = (LicenseRenewal.objects
                .filter(license_id=self.license_id, renewed_at__lt=self.renewed_at)
                .order_by('-renewed_at').first())
        base = prev.total_cost if prev else None
        if base is None:
            return Decimal('0.00')
        return _money(self.total_cost - base)


# ════════════════════════════════════════════════════════════════════════════
# Audit trail
# ════════════════════════════════════════════════════════════════════════════

class LicenseEvent(models.Model):
    """Append-only audit trail — same shape as AssetEvent."""

    class Kind(models.TextChoices):
        CREATED       = 'created',       'Created'
        UPDATED       = 'updated',       'Updated'
        SEAT_ASSIGNED = 'seat_assigned', 'Seat assigned'
        SEAT_RELEASED = 'seat_released', 'Seat released'
        RENEWED       = 'renewed',       'Renewed'
        SUSPENDED     = 'suspended',     'Suspended'
        REACTIVATED   = 'reactivated',   'Reactivated'
        CANCELLED     = 'cancelled',     'Cancelled'
        EXPIRED       = 'expired',       'Expired'
        REMINDER      = 'reminder',      'Reminder sent'
        NOTE          = 'note',          'Note'

    id         = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    license    = models.ForeignKey(License, on_delete=models.CASCADE, related_name='events')
    kind       = models.CharField(max_length=16, choices=Kind.choices)
    actor      = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name='license_events',
    )
    message    = models.CharField(max_length=400)
    payload    = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Licence event'

    def __str__(self):
        return f'{self.license.reference}: {self.get_kind_display()}'

    @property
    def icon(self) -> str:
        return {
            'created':       'bi-plus-circle',
            'updated':       'bi-pencil',
            'seat_assigned': 'bi-person-plus',
            'seat_released': 'bi-person-dash',
            'renewed':       'bi-arrow-repeat',
            'suspended':     'bi-pause-circle',
            'reactivated':   'bi-play-circle',
            'cancelled':     'bi-x-circle',
            'expired':       'bi-clock-history',
            'reminder':      'bi-bell',
            'note':          'bi-chat-left-text',
        }.get(self.kind, 'bi-dot')
