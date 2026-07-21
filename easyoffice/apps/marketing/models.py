"""
apps/marketing/models.py
========================

Marketing module — Campaigns, Spend, Contacts (lead pipeline) and the
CAC / LTV analytics engine.

Concepts
--------
MarketingCampaign
    A named marketing effort (Facebook Ads, radio, referral drive, …)
    with a planned budget and a period. Actual spend is recorded as
    itemized CampaignExpense rows so CAC is based on REAL money out,
    not the plan.

CampaignExpense
    One line of real spend against a campaign (date, amount, category).

MarketingContact
    A person/company entered by the marketing team. Moves through a
    pipeline: LEAD → QUALIFIED → CUSTOMER (or LOST). When a contact is
    marked CUSTOMER, `converted_at` is stamped — that is the acquisition
    date used for CAC.

ContactActivity
    A touch log on a contact (call, email, meeting, note).

CAC (Customer Acquisition Cost)
    Per campaign:  total real spend on the campaign
                   ÷ contacts converted to CUSTOMER attributed to it.
    Blended:       total marketing spend in a period
                   ÷ all customers acquired in that period.

LTV (Lifetime Value)
    Revenue from finalized InvoiceDocuments (apps.invoices) matched to a
    contact by client_email (primary) or exact client_name against the
    contact's company / full name. Basis mirrors the Sales Targets
    module: 'invoiced' (invoice_date) or 'paid' (paid_at).

IMPORTANT — installation:
    1. Add 'apps.marketing' to INSTALLED_APPS in settings.
    2. python manage.py makemigrations marketing && python manage.py migrate
    3. Create a "Marketing" auth group and add your marketing staff to it.
       CEO / Admin groups (and superusers) automatically have access.
"""
import uuid
from datetime import date
from decimal import Decimal

from django.conf import settings
from django.db import models
from django.db.models import Q, Sum
from django.utils import timezone


User = settings.AUTH_USER_MODEL


# ═════════════════════════════════════════════════════════════════════════
# Campaigns
# ═════════════════════════════════════════════════════════════════════════

class MarketingCampaign(models.Model):
    """A marketing campaign with a budget, a period and real recorded spend."""

    class Channel(models.TextChoices):
        SOCIAL_MEDIA  = 'social_media',  'Social Media (Facebook / Instagram / TikTok)'
        SEARCH_ADS    = 'search_ads',    'Search / Google Ads'
        RADIO         = 'radio',         'Radio'
        TV            = 'tv',            'TV'
        PRINT         = 'print',         'Print / Flyers / Billboards'
        EMAIL         = 'email',         'Email Marketing'
        SMS           = 'sms',           'SMS / WhatsApp'
        EVENTS        = 'events',        'Events / Exhibitions'
        REFERRAL      = 'referral',      'Referral Program'
        PARTNERSHIP   = 'partnership',   'Partnership'
        DIRECT_SALES  = 'direct_sales',  'Direct Sales Outreach'
        OTHER         = 'other',         'Other'

    class Status(models.TextChoices):
        PLANNED   = 'planned',   'Planned'
        ACTIVE    = 'active',    'Active'
        PAUSED    = 'paused',    'Paused'
        COMPLETED = 'completed', 'Completed'
        CANCELLED = 'cancelled', 'Cancelled'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    name        = models.CharField(max_length=200)
    channel     = models.CharField(max_length=30, choices=Channel.choices,
                                   default=Channel.SOCIAL_MEDIA)
    status      = models.CharField(max_length=20, choices=Status.choices,
                                   default=Status.PLANNED)
    objective   = models.CharField(
        max_length=300, blank=True,
        help_text='What this campaign is trying to achieve.',
    )

    start_date  = models.DateField()
    end_date    = models.DateField(null=True, blank=True,
                                   help_text='Leave empty for ongoing campaigns.')

    budget      = models.DecimalField(
        max_digits=14, decimal_places=2, default=Decimal('0.00'),
        help_text='Planned budget. Actual spend is recorded as expenses.',
    )
    currency    = models.CharField(max_length=8, default='GMD')

    utm_code    = models.CharField(
        max_length=60, blank=True,
        help_text='Optional tracking code (e.g. UTM campaign tag or promo code).',
    )
    target_audience = models.CharField(max_length=300, blank=True)
    notes       = models.TextField(blank=True)

    owner       = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='marketing_campaigns_owned',
        help_text='Marketing staff member responsible for this campaign.',
    )
    created_by  = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='marketing_campaigns_created',
    )
    created_at  = models.DateTimeField(auto_now_add=True)
    updated_at  = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-start_date', 'name']
        indexes = [
            models.Index(fields=['status', 'channel']),
            models.Index(fields=['start_date']),
        ]

    def __str__(self):
        return f'{self.name} ({self.get_channel_display()})'

    # ── Spend ────────────────────────────────────────────────────────────

    def total_spend(self) -> Decimal:
        """Real money out — the sum of itemized expenses."""
        total = self.expenses.aggregate(s=Sum('amount'))['s']
        return total or Decimal('0.00')

    def budget_utilization_pct(self) -> float:
        if not self.budget:
            return 0.0
        return min(float(self.total_spend() / self.budget * 100), 999.0)

    # ── Attribution ──────────────────────────────────────────────────────

    def contacts_qs(self):
        return self.contacts.all()

    def leads_count(self) -> int:
        return self.contacts.count()

    def customers_qs(self):
        return self.contacts.filter(stage=MarketingContact.Stage.CUSTOMER)

    def customers_count(self) -> int:
        return self.customers_qs().count()

    def conversion_rate_pct(self) -> float:
        leads = self.leads_count()
        if not leads:
            return 0.0
        return round(self.customers_count() / leads * 100, 1)

    # ── CAC ──────────────────────────────────────────────────────────────

    def cac(self) -> Decimal | None:
        """
        Campaign CAC = total real spend ÷ customers acquired via this
        campaign. None when there is spend but no customer yet (CAC is
        undefined — the money bought zero customers so far).
        """
        customers = self.customers_count()
        if customers == 0:
            return None
        return (self.total_spend() / customers).quantize(Decimal('0.01'))

    # ── Revenue / ROI ────────────────────────────────────────────────────

    def attributed_revenue(self, basis: str = 'invoiced') -> Decimal:
        """Sum of LTV across every contact attributed to this campaign."""
        total = Decimal('0.00')
        for contact in self.contacts.all():
            total += contact.lifetime_value(basis=basis)
        return total

    def roi_pct(self, basis: str = 'invoiced') -> float | None:
        """(revenue − spend) / spend × 100. None when nothing was spent."""
        spend = self.total_spend()
        if not spend:
            return None
        revenue = self.attributed_revenue(basis=basis)
        return round(float((revenue - spend) / spend * 100), 1)

    @property
    def is_running(self) -> bool:
        today = timezone.localdate()
        if self.status != self.Status.ACTIVE:
            return False
        if self.end_date and today > self.end_date:
            return False
        return self.start_date <= today


class CampaignExpense(models.Model):
    """One line of real spend against a campaign."""

    class Category(models.TextChoices):
        AD_SPEND     = 'ad_spend',     'Ad Spend'
        AIRTIME      = 'airtime',      'Radio / TV Airtime'
        PRINTING     = 'printing',     'Printing & Materials'
        AGENCY       = 'agency',       'Agency / Consultant Fees'
        EVENT_COSTS  = 'event_costs',  'Event Costs'
        INCENTIVES   = 'incentives',   'Referral Incentives / Promos'
        SOFTWARE     = 'software',     'Software / Tools'
        TRANSPORT    = 'transport',    'Transport & Logistics'
        OTHER        = 'other',        'Other'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    campaign = models.ForeignKey(
        MarketingCampaign, on_delete=models.CASCADE, related_name='expenses',
    )
    date        = models.DateField(default=timezone.localdate)
    amount      = models.DecimalField(max_digits=14, decimal_places=2)
    category    = models.CharField(max_length=30, choices=Category.choices,
                                   default=Category.AD_SPEND)
    description = models.CharField(max_length=300, blank=True)
    receipt     = models.FileField(upload_to='marketing/receipts/%Y/%m/',
                                   null=True, blank=True)
    recorded_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='marketing_expenses_recorded',
    )
    created_at  = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-date', '-created_at']

    def __str__(self):
        return f'{self.campaign.name} — {self.get_category_display()} — {self.amount}'


# ═════════════════════════════════════════════════════════════════════════
# Contacts (lead → customer pipeline)
# ═════════════════════════════════════════════════════════════════════════

class MarketingContact(models.Model):
    """
    A customer contact entered by marketing. Moves through the pipeline
    and, once CUSTOMER, is matched against finalized invoices for LTV.
    """

    class Stage(models.TextChoices):
        LEAD      = 'lead',      'Lead'
        QUALIFIED = 'qualified', 'Qualified'
        CUSTOMER  = 'customer',  'Customer'
        LOST      = 'lost',      'Lost'

    class Source(models.TextChoices):
        CAMPAIGN     = 'campaign',     'Marketing Campaign'
        WALK_IN      = 'walk_in',      'Walk-in'
        REFERRAL     = 'referral',     'Referral'
        WEBSITE      = 'website',      'Website'
        SOCIAL_MEDIA = 'social_media', 'Social Media (organic)'
        PHONE_IN     = 'phone_in',     'Inbound Call'
        EVENT        = 'event',        'Event'
        EXISTING     = 'existing',     'Existing Relationship'
        OTHER        = 'other',        'Other'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # ── Identity ─────────────────────────────────────────────────────────
    full_name  = models.CharField(max_length=200)
    company    = models.CharField(max_length=200, blank=True)
    job_title  = models.CharField(max_length=120, blank=True)
    email      = models.EmailField(blank=True)
    phone      = models.CharField(max_length=50, blank=True)
    address    = models.TextField(blank=True)

    # ── Pipeline ─────────────────────────────────────────────────────────
    stage        = models.CharField(max_length=20, choices=Stage.choices,
                                    default=Stage.LEAD)
    source       = models.CharField(max_length=30, choices=Source.choices,
                                    default=Source.CAMPAIGN)
    campaign     = models.ForeignKey(
        MarketingCampaign, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='contacts',
        help_text='The campaign this contact is attributed to (for CAC).',
    )
    converted_at = models.DateTimeField(
        null=True, blank=True,
        help_text='Stamped automatically when the contact becomes a Customer.',
    )
    lost_reason  = models.CharField(max_length=300, blank=True)

    # ── Ownership / follow-up ────────────────────────────────────────────
    assigned_to    = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='marketing_contacts_assigned',
    )
    next_follow_up = models.DateField(null=True, blank=True)
    interest       = models.CharField(
        max_length=300, blank=True,
        help_text='Products / services the contact is interested in.',
    )
    notes          = models.TextField(blank=True)

    created_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='marketing_contacts_created',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['stage']),
            models.Index(fields=['email']),
        ]

    def __str__(self):
        return f'{self.full_name}' + (f' ({self.company})' if self.company else '')

    # ── Stage transitions ────────────────────────────────────────────────

    def set_stage(self, new_stage: str, lost_reason: str = ''):
        """Central stage-change so converted_at is stamped consistently."""
        self.stage = new_stage
        if new_stage == self.Stage.CUSTOMER and not self.converted_at:
            self.converted_at = timezone.now()
        if new_stage == self.Stage.LOST:
            self.lost_reason = lost_reason or self.lost_reason
        self.save(update_fields=['stage', 'converted_at', 'lost_reason',
                                 'updated_at'])

    @property
    def is_customer(self) -> bool:
        return self.stage == self.Stage.CUSTOMER

    @property
    def days_since_created(self) -> int:
        return (timezone.now() - self.created_at).days

    @property
    def follow_up_overdue(self) -> bool:
        return bool(
            self.next_follow_up
            and self.stage in (self.Stage.LEAD, self.Stage.QUALIFIED)
            and self.next_follow_up < timezone.localdate()
        )

    # ── LTV ──────────────────────────────────────────────────────────────

    def matched_invoices_qs(self, basis: str = 'invoiced'):
        """
        Finalized billable invoices belonging to this contact, matched by
        client_email (primary) or exact client_name against the contact's
        company or full name. Lazy import keeps the marketing ↔ invoices
        coupling at runtime only.
        """
        from apps.invoices.models import InvoiceDocument, DocType

        match = Q(pk=None)  # matches nothing
        if self.email:
            match |= Q(client_email__iexact=self.email.strip())
        if self.company:
            match |= Q(client_name__iexact=self.company.strip())
        if self.full_name:
            match |= Q(client_name__iexact=self.full_name.strip())

        qs = InvoiceDocument.objects.filter(
            match,
            doc_type=DocType.INVOICE,
            status=InvoiceDocument.Status.FINALIZED,
        )
        if basis == 'paid':
            qs = qs.filter(paid_at__isnull=False)
        return qs

    def lifetime_value(self, basis: str = 'invoiced') -> Decimal:
        total = self.matched_invoices_qs(basis=basis).aggregate(
            s=Sum('total'))['s']
        return total or Decimal('0.00')

    def invoice_count(self, basis: str = 'invoiced') -> int:
        return self.matched_invoices_qs(basis=basis).count()


class ContactActivity(models.Model):
    """A touch on a contact — call, email, meeting, WhatsApp, note."""

    class Kind(models.TextChoices):
        CALL     = 'call',     'Phone Call'
        EMAIL    = 'email',    'Email'
        MEETING  = 'meeting',  'Meeting / Visit'
        WHATSAPP = 'whatsapp', 'WhatsApp / SMS'
        NOTE     = 'note',     'Note'
        STAGE    = 'stage',    'Stage Change'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    contact = models.ForeignKey(
        MarketingContact, on_delete=models.CASCADE, related_name='activities',
    )
    kind    = models.CharField(max_length=20, choices=Kind.choices,
                               default=Kind.NOTE)
    summary = models.CharField(max_length=300)
    details = models.TextField(blank=True)
    logged_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='marketing_activities_logged',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name_plural = 'contact activities'

    def __str__(self):
        return f'{self.get_kind_display()} — {self.contact.full_name}'


# ═════════════════════════════════════════════════════════════════════════
# Analytics helpers (module-level, used by the dashboard)
# ═════════════════════════════════════════════════════════════════════════

def blended_cac(date_from: date, date_to: date,
                currency: str = 'GMD') -> dict:
    """
    Blended CAC across ALL marketing activity in a period:

        total spend (all campaign expenses dated in the period, in currency)
        ÷ customers converted in the period

    Returns {'spend', 'customers', 'cac'} — cac is None when no customer
    was acquired in the period.
    """
    spend = CampaignExpense.objects.filter(
        date__gte=date_from, date__lte=date_to,
        campaign__currency=currency,
    ).aggregate(s=Sum('amount'))['s'] or Decimal('0.00')

    customers = MarketingContact.objects.filter(
        stage=MarketingContact.Stage.CUSTOMER,
        converted_at__date__gte=date_from,
        converted_at__date__lte=date_to,
    ).count()

    cac = (spend / customers).quantize(Decimal('0.01')) if customers else None
    return {'spend': spend, 'customers': customers, 'cac': cac}


def average_ltv(basis: str = 'invoiced', currency: str = 'GMD') -> dict:
    """
    Average LTV across every converted customer whose matched invoices are
    in the given currency. Returns {'customers', 'total_revenue', 'avg_ltv'}.
    """
    total = Decimal('0.00')
    counted = 0
    for contact in MarketingContact.objects.filter(
            stage=MarketingContact.Stage.CUSTOMER):
        ltv = contact.matched_invoices_qs(basis=basis).filter(
            currency=currency).aggregate(s=Sum('total'))['s'] or Decimal('0.00')
        total += ltv
        counted += 1
    avg = (total / counted).quantize(Decimal('0.01')) if counted else None
    return {'customers': counted, 'total_revenue': total, 'avg_ltv': avg}
