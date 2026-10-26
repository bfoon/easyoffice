"""
apps/investments/models.py
──────────────────────────
Investor onboarding → wallet → holdings in listed opportunities → profit
sharing from the sales of the items each opportunity is tied to.

Money rules
  • Every wallet movement is a WalletTransaction (append-only) and
    InvestorAccount.wallet_balance is a cached running total updated in the
    same DB transaction, under select_for_update on the account row.
  • Every change to a holding's capital is a HoldingEvent with
    capital_after, which is what time-weighted profit allocation integrates.
  • Distributions are unique per (opportunity, period_start), so re-running
    the job can never pay a period twice.
"""
import os
from datetime import timedelta
import secrets
import uuid
from decimal import Decimal

from django.conf import settings
from django.core.files.storage import FileSystemStorage
from django.db import models
from django.utils import timezone
from django.utils.text import slugify

from apps.investments import conf

ZERO = Decimal('0.00')


# ─────────────────────────────────────────────────────────────────────────────
# Private storage (KYC documents, deposit proofs) — never under a public URL.
# Files get random names and are streamed only by permission-checking views.
# ─────────────────────────────────────────────────────────────────────────────

def private_storage():
    return FileSystemStorage(location=conf.private_root(), base_url=None)


def _private_name(folder, filename):
    ext = os.path.splitext(filename)[1].lower()[:10]
    return f'{folder}/{timezone.now():%Y/%m}/{uuid.uuid4().hex}{ext}'


def kyc_upload_to(instance, filename):
    return _private_name('kyc', filename)


def proof_upload_to(instance, filename):
    return _private_name('deposit_proofs', filename)


def _token():
    return secrets.token_urlsafe(32)


def _account_no():
    return f'EI{timezone.now():%y}{secrets.randbelow(10 ** 6):06d}'


class TimeStamped(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class PayoutMethod(models.TextChoices):
    BANK = 'bank', 'Bank transfer'
    WAVE = 'wave', 'Wave'
    QMONEY = 'qmoney', 'QMoney'
    AFRIMONEY = 'afrimoney', 'Afrimoney'
    CASH = 'cash', 'Cash at office'


# ═════════════════════════════════════════════════════════════════════════════
# Onboarding
# ═════════════════════════════════════════════════════════════════════════════

class InvestorInvitation(TimeStamped):
    class Status(models.TextChoices):
        SENT = 'sent', 'Sent'
        OPENED = 'opened', 'Opened'
        SUBMITTED = 'submitted', 'Application submitted'
        REVOKED = 'revoked', 'Revoked'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    email = models.EmailField(db_index=True)
    full_name = models.CharField(max_length=180, blank=True)
    phone = models.CharField(max_length=30, blank=True)
    personal_message = models.TextField(blank=True)
    token = models.CharField(max_length=64, unique=True, default=_token, editable=False)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.SENT)
    expires_at = models.DateTimeField()
    invited_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                   null=True, blank=True, related_name='investor_invitations')
    opened_at = models.DateTimeField(null=True, blank=True)
    sent_count = models.PositiveIntegerField(default=0)
    last_sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'Invite → {self.email}'

    def save(self, *args, **kwargs):
        if not self.expires_at:
            self.expires_at = timezone.now() + timedelta(days=conf.invite_days())
        super().save(*args, **kwargs)

    @property
    def is_expired(self):
        return timezone.now() >= self.expires_at

    @property
    def is_usable(self):
        return self.status in (self.Status.SENT, self.Status.OPENED) and not self.is_expired

    @property
    def public_url(self):
        from django.urls import reverse
        return conf.site_url() + reverse('invest:apply', args=[self.token])


class InvestorApplication(TimeStamped):
    class Status(models.TextChoices):
        SUBMITTED = 'submitted', 'Awaiting review'
        INFO_REQUESTED = 'info_requested', 'More information requested'
        APPROVED = 'approved', 'Approved'
        REJECTED = 'rejected', 'Rejected'

    class IdType(models.TextChoices):
        NATIONAL_ID = 'national_id', 'National ID card'
        PASSPORT = 'passport', 'Passport'
        DRIVERS = 'drivers', "Driver's licence"
        RESIDENCE = 'residence', 'Residence permit'

    class Funds(models.TextChoices):
        SALARY = 'salary', 'Salary / employment'
        BUSINESS = 'business', 'Business income'
        SAVINGS = 'savings', 'Savings'
        REMITTANCE = 'remittance', 'Remittances from abroad'
        INHERITANCE = 'inheritance', 'Inheritance / gift'
        OTHER = 'other', 'Other'

    class Risk(models.TextChoices):
        LOW = 'low', 'Low — protect my capital'
        MEDIUM = 'medium', 'Medium — balanced'
        HIGH = 'high', 'High — maximise growth'

    class Horizon(models.TextChoices):
        SHORT = 'short', 'Under 6 months'
        MID = 'mid', '6–12 months'
        LONG = 'long', 'More than a year'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    invitation = models.OneToOneField(InvestorInvitation, on_delete=models.SET_NULL,
                                      null=True, blank=True, related_name='application')
    status = models.CharField(max_length=16, choices=Status.choices,
                              default=Status.SUBMITTED, db_index=True)

    # Identity
    full_name = models.CharField(max_length=180)
    email = models.EmailField(db_index=True)
    phone = models.CharField(max_length=30)
    date_of_birth = models.DateField(null=True, blank=True)
    nationality = models.CharField(max_length=80, blank=True)
    address = models.CharField(max_length=300, blank=True)
    city = models.CharField(max_length=80, blank=True)
    country = models.CharField(max_length=80, default='The Gambia')
    occupation = models.CharField(max_length=120, blank=True)
    employer = models.CharField(max_length=160, blank=True)

    # KYC
    id_type = models.CharField(max_length=12, choices=IdType.choices, default=IdType.NATIONAL_ID)
    id_number = models.CharField(max_length=60)
    id_document = models.FileField(upload_to=kyc_upload_to, storage=private_storage,
                                   max_length=255, blank=True)
    proof_of_address = models.FileField(upload_to=kyc_upload_to, storage=private_storage,
                                        max_length=255, blank=True)
    source_of_funds = models.CharField(max_length=12, choices=Funds.choices, default=Funds.SALARY)
    source_of_funds_detail = models.CharField(max_length=300, blank=True)

    # Profile
    expected_amount = models.DecimalField(max_digits=14, decimal_places=2, default=ZERO)
    risk_tolerance = models.CharField(max_length=8, choices=Risk.choices, default=Risk.MEDIUM)
    horizon = models.CharField(max_length=8, choices=Horizon.choices, default=Horizon.MID)

    next_of_kin_name = models.CharField(max_length=180, blank=True)
    next_of_kin_phone = models.CharField(max_length=30, blank=True)
    next_of_kin_relationship = models.CharField(max_length=60, blank=True)

    payout_method = models.CharField(max_length=12, choices=PayoutMethod.choices,
                                     default=PayoutMethod.WAVE)
    payout_account_name = models.CharField(max_length=180, blank=True)
    payout_account_number = models.CharField(max_length=60, blank=True)
    payout_bank_name = models.CharField(max_length=120, blank=True)

    agreed_terms = models.BooleanField(default=False)
    signature_name = models.CharField(max_length=180, blank=True)
    signed_at = models.DateTimeField(null=True, blank=True)
    signed_ip = models.GenericIPAddressField(null=True, blank=True)

    # Review
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                    null=True, blank=True, related_name='+')
    reviewed_at = models.DateTimeField(null=True, blank=True)
    review_note = models.TextField(blank=True)
    info_request_note = models.TextField(blank=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.full_name} ({self.get_status_display()})'


class InvestorAccount(TimeStamped):
    class Status(models.TextChoices):
        PENDING_ACTIVATION = 'pending', 'Awaiting activation'
        ACTIVE = 'active', 'Active'
        SUSPENDED = 'suspended', 'Suspended'
        CLOSED = 'closed', 'Closed'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
                                related_name='investor_account')
    application = models.OneToOneField(InvestorApplication, on_delete=models.SET_NULL,
                                       null=True, blank=True, related_name='account')
    account_no = models.CharField(max_length=20, unique=True, default=_account_no)
    status = models.CharField(max_length=10, choices=Status.choices,
                              default=Status.PENDING_ACTIVATION, db_index=True)
    portal_only = models.BooleanField(
        default=True,
        help_text='External investor: confined to the investor portal. '
                  'False for staff who also invest.',
    )
    wallet_balance = models.DecimalField(max_digits=16, decimal_places=2, default=ZERO)

    risk_tolerance = models.CharField(max_length=8, choices=InvestorApplication.Risk.choices,
                                      default=InvestorApplication.Risk.MEDIUM)
    payout_method = models.CharField(max_length=12, choices=PayoutMethod.choices,
                                     default=PayoutMethod.WAVE)
    payout_account_name = models.CharField(max_length=180, blank=True)
    payout_account_number = models.CharField(max_length=60, blank=True)
    payout_bank_name = models.CharField(max_length=120, blank=True)

    activation_token = models.CharField(max_length=64, blank=True, db_index=True)
    activation_expires = models.DateTimeField(null=True, blank=True)
    activated_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                    null=True, blank=True, related_name='+')

    # Investor-chosen savings goal shown on the dashboard
    goal_amount = models.DecimalField(max_digits=16, decimal_places=2, null=True, blank=True)
    goal_label = models.CharField(max_length=80, blank=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.account_no} — {self.display_name}'

    @property
    def display_name(self):
        u = self.user
        return (u.get_full_name() or u.email or str(u.pk)).strip()

    @property
    def is_active(self):
        return self.status == self.Status.ACTIVE


class WalletTransaction(models.Model):
    class Kind(models.TextChoices):
        DEPOSIT = 'deposit', 'Deposit'
        WITHDRAWAL = 'withdrawal', 'Withdrawal'
        WITHDRAWAL_REVERSAL = 'withdrawal_reversal', 'Withdrawal returned'
        INVEST = 'invest', 'Invested'
        REDEEM = 'redeem', 'Capital returned'
        PROFIT = 'profit', 'Profit payout'
        FEE = 'fee', 'Fee'
        ADJUSTMENT = 'adjustment', 'Adjustment'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    account = models.ForeignKey(InvestorAccount, on_delete=models.PROTECT,
                                related_name='wallet_transactions')
    kind = models.CharField(max_length=20, choices=Kind.choices, db_index=True)
    amount = models.DecimalField(max_digits=16, decimal_places=2,
                                 help_text='Signed: credits positive, debits negative.')
    balance_after = models.DecimalField(max_digits=16, decimal_places=2)
    holding = models.ForeignKey('Holding', on_delete=models.SET_NULL, null=True, blank=True,
                                related_name='wallet_transactions')
    reference = models.CharField(max_length=80, blank=True)
    description = models.CharField(max_length=300, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                   null=True, blank=True, related_name='+')
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [models.Index(fields=['account', '-created_at'])]

    def __str__(self):
        return f'{self.kind} {self.amount}'


class DepositRequest(TimeStamped):
    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending confirmation'
        CONFIRMED = 'confirmed', 'Confirmed'
        REJECTED = 'rejected', 'Rejected'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    account = models.ForeignKey(InvestorAccount, on_delete=models.PROTECT, related_name='deposits')
    amount = models.DecimalField(max_digits=16, decimal_places=2)
    method = models.CharField(max_length=12, choices=PayoutMethod.choices)
    reference = models.CharField(max_length=80, blank=True,
                                 help_text='Mobile money / bank transaction ID')
    proof = models.FileField(upload_to=proof_upload_to, storage=private_storage,
                             max_length=255, blank=True)
    note = models.CharField(max_length=300, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING,
                              db_index=True)
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                    null=True, blank=True, related_name='+')
    reviewed_at = models.DateTimeField(null=True, blank=True)
    review_note = models.CharField(max_length=300, blank=True)
    wallet_txn = models.OneToOneField(WalletTransaction, on_delete=models.SET_NULL,
                                      null=True, blank=True, related_name='deposit')

    class Meta:
        ordering = ['-created_at']


class WithdrawalRequest(TimeStamped):
    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending'
        PAID = 'paid', 'Paid out'
        REJECTED = 'rejected', 'Rejected'
        CANCELLED = 'cancelled', 'Cancelled'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    account = models.ForeignKey(InvestorAccount, on_delete=models.PROTECT, related_name='withdrawals')
    amount = models.DecimalField(max_digits=16, decimal_places=2)
    method = models.CharField(max_length=12, choices=PayoutMethod.choices)
    account_name = models.CharField(max_length=180, blank=True)
    account_number = models.CharField(max_length=60, blank=True)
    bank_name = models.CharField(max_length=120, blank=True)
    note = models.CharField(max_length=300, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING,
                              db_index=True)
    payout_reference = models.CharField(max_length=80, blank=True)
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                    null=True, blank=True, related_name='+')
    reviewed_at = models.DateTimeField(null=True, blank=True)
    review_note = models.CharField(max_length=300, blank=True)
    hold_txn = models.OneToOneField(WalletTransaction, on_delete=models.SET_NULL,
                                    null=True, blank=True, related_name='withdrawal_hold')

    class Meta:
        ordering = ['-created_at']


# ═════════════════════════════════════════════════════════════════════════════
# Listings
# ═════════════════════════════════════════════════════════════════════════════

class InvestmentOpportunity(TimeStamped):
    class Status(models.TextChoices):
        DRAFT = 'draft', 'Draft'
        OPEN = 'open', 'Open for investment'
        PAUSED = 'paused', 'Paused (no new money)'
        CLOSED = 'closed', 'Closed'

    class Risk(models.TextChoices):
        LOW = 'low', 'Low'
        MEDIUM = 'medium', 'Medium'
        HIGH = 'high', 'High'

    class Frequency(models.TextChoices):
        DAILY = 'daily', 'Daily'
        WEEKLY = 'weekly', 'Weekly'
        MONTHLY = 'monthly', 'Monthly'

    class LossPolicy(models.TextChoices):
        ABSORB = 'absorb', 'Company absorbs losses'
        SHARE = 'share', 'Investors share losses'

    class InvoiceBasis(models.TextChoices):
        PAID = 'paid', 'When the invoice is paid'
        FINALIZED = 'finalized', 'When the invoice is finalized'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    title = models.CharField(max_length=160)
    slug = models.SlugField(max_length=180, unique=True, blank=True)
    tagline = models.CharField(max_length=220, blank=True)
    description = models.TextField(blank=True)
    cover_image = models.ImageField(upload_to='investments/covers/', blank=True)
    category = models.CharField(max_length=80, blank=True)
    risk_level = models.CharField(max_length=8, choices=Risk.choices, default=Risk.MEDIUM)
    status = models.CharField(max_length=8, choices=Status.choices, default=Status.DRAFT,
                              db_index=True)

    products = models.ManyToManyField('inventory.Product', blank=True,
                                      related_name='investment_opportunities')

    investor_share_pct = models.DecimalField(
        max_digits=5, decimal_places=2, default=Decimal('40.00'),
        help_text='Share of net profit from these items paid to investors, at full funding.')
    scale_by_funding = models.BooleanField(
        default=True,
        help_text='If only half the target is raised, investors get half the share.')
    target_capital = models.DecimalField(max_digits=16, decimal_places=2, default=ZERO)
    min_investment = models.DecimalField(max_digits=16, decimal_places=2, default=Decimal('500.00'))
    max_per_investor = models.DecimalField(max_digits=16, decimal_places=2, null=True, blank=True)
    allow_overfunding = models.BooleanField(default=False)

    lock_in_days = models.PositiveIntegerField(default=30)
    early_exit_fee_pct = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('2.00'))

    distribution_frequency = models.CharField(max_length=8, choices=Frequency.choices,
                                              default=Frequency.MONTHLY)
    loss_policy = models.CharField(max_length=8, choices=LossPolicy.choices,
                                   default=LossPolicy.ABSORB)
    auto_post = models.BooleanField(
        default=False,
        help_text='Post distributions automatically. Off = a manager reviews each one first.')

    sales_start = models.DateField(default=timezone.localdate,
                                   help_text='Only sales from this date count.')
    include_pos = models.BooleanField(default=True)
    include_invoices = models.BooleanField(default=True)
    invoice_basis = models.CharField(max_length=10, choices=InvoiceBasis.choices,
                                     default=InvoiceBasis.PAID)

    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                   null=True, blank=True, related_name='+')

    class Meta:
        ordering = ['-created_at']
        verbose_name_plural = 'investment opportunities'

    def __str__(self):
        return self.title

    def save(self, *args, **kwargs):
        if not self.slug:
            base = slugify(self.title)[:150] or 'opportunity'
            slug, n = base, 2
            while InvestmentOpportunity.objects.filter(slug=slug).exclude(pk=self.pk).exists():
                slug, n = f'{base}-{n}', n + 1
            self.slug = slug
        super().save(*args, **kwargs)

    @property
    def is_investable(self):
        return self.status == self.Status.OPEN

    def raised(self):
        return self.holdings.filter(status=Holding.Status.ACTIVE).aggregate(
            s=models.Sum('capital'))['s'] or ZERO

    def investor_count(self):
        return self.holdings.filter(status=Holding.Status.ACTIVE, capital__gt=0).count()

    def funded_pct(self):
        if not self.target_capital:
            return Decimal('0')
        return min(Decimal('100'), (self.raised() / self.target_capital * 100).quantize(Decimal('0.1')))

    def remaining_capacity(self):
        if self.allow_overfunding or not self.target_capital:
            return None
        return max(ZERO, self.target_capital - self.raised())


class OpportunityUpdate(models.Model):
    """News posted to investors in an opportunity ('New stock arrived')."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    opportunity = models.ForeignKey(InvestmentOpportunity, on_delete=models.CASCADE,
                                    related_name='updates')
    title = models.CharField(max_length=160)
    body = models.TextField()
    posted_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                  null=True, blank=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']


class ProfitAdjustment(models.Model):
    """Revenue or expense that doesn't come from POS/invoices — shipping,
    customs, storage, a bulk sale recorded elsewhere."""

    class Kind(models.TextChoices):
        REVENUE = 'revenue', 'Extra revenue'
        EXPENSE = 'expense', 'Expense'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    opportunity = models.ForeignKey(InvestmentOpportunity, on_delete=models.CASCADE,
                                    related_name='adjustments')
    date = models.DateField(default=timezone.localdate)
    kind = models.CharField(max_length=8, choices=Kind.choices, default=Kind.EXPENSE)
    amount = models.DecimalField(max_digits=16, decimal_places=2)
    description = models.CharField(max_length=300)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                   null=True, blank=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-date', '-created_at']

    @property
    def signed_amount(self):
        return self.amount if self.kind == self.Kind.REVENUE else -self.amount


# ═════════════════════════════════════════════════════════════════════════════
# Holdings
# ═════════════════════════════════════════════════════════════════════════════

class Holding(TimeStamped):
    class Status(models.TextChoices):
        ACTIVE = 'active', 'Active'
        CLOSED = 'closed', 'Closed'

    class PayoutMode(models.TextChoices):
        REINVEST = 'reinvest', 'Reinvest profit (compound)'
        WALLET = 'wallet', 'Pay profit to wallet'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    account = models.ForeignKey(InvestorAccount, on_delete=models.PROTECT, related_name='holdings')
    opportunity = models.ForeignKey(InvestmentOpportunity, on_delete=models.PROTECT,
                                    related_name='holdings')
    status = models.CharField(max_length=8, choices=Status.choices, default=Status.ACTIVE)
    payout_mode = models.CharField(max_length=10, choices=PayoutMode.choices,
                                   default=PayoutMode.REINVEST)

    capital = models.DecimalField(max_digits=16, decimal_places=2, default=ZERO)
    principal_contributed = models.DecimalField(max_digits=16, decimal_places=2, default=ZERO)
    redeemed_total = models.DecimalField(max_digits=16, decimal_places=2, default=ZERO)
    profit_total = models.DecimalField(max_digits=16, decimal_places=2, default=ZERO)
    profit_reinvested = models.DecimalField(max_digits=16, decimal_places=2, default=ZERO)
    profit_paid_out = models.DecimalField(max_digits=16, decimal_places=2, default=ZERO)
    losses_total = models.DecimalField(max_digits=16, decimal_places=2, default=ZERO)
    fees_total = models.DecimalField(max_digits=16, decimal_places=2, default=ZERO)

    first_invested_at = models.DateTimeField(null=True, blank=True)
    last_invested_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']
        constraints = [
            models.UniqueConstraint(fields=['account', 'opportunity'], name='uniq_holding_per_opportunity'),
        ]

    def __str__(self):
        return f'{self.account.account_no} · {self.opportunity.title}'

    @property
    def lock_ends_at(self):
        if not self.last_invested_at:
            return None
        return self.last_invested_at + timedelta(days=self.opportunity.lock_in_days)

    @property
    def is_locked(self):
        end = self.lock_ends_at
        return bool(end and timezone.now() < end)

    @property
    def net_gain(self):
        return self.profit_total - self.losses_total - self.fees_total

    @property
    def roi_pct(self):
        if not self.principal_contributed:
            return Decimal('0')
        return (self.net_gain / self.principal_contributed * 100).quantize(Decimal('0.01'))


class HoldingEvent(models.Model):
    class Kind(models.TextChoices):
        INVEST = 'invest', 'Invested'
        REDEEM = 'redeem', 'Withdrew capital'
        PROFIT_REINVEST = 'profit_reinvest', 'Profit reinvested'
        PROFIT_PAID = 'profit_paid', 'Profit paid to wallet'
        LOSS = 'loss', 'Loss share'
        FEE = 'fee', 'Early-exit fee'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    holding = models.ForeignKey(Holding, on_delete=models.CASCADE, related_name='events')
    kind = models.CharField(max_length=16, choices=Kind.choices)
    amount = models.DecimalField(max_digits=16, decimal_places=2)
    capital_delta = models.DecimalField(max_digits=16, decimal_places=2, default=ZERO)
    capital_after = models.DecimalField(max_digits=16, decimal_places=2)
    distribution = models.ForeignKey('Distribution', on_delete=models.SET_NULL,
                                     null=True, blank=True, related_name='holding_events')
    wallet_txn = models.ForeignKey(WalletTransaction, on_delete=models.SET_NULL,
                                   null=True, blank=True, related_name='+')
    note = models.CharField(max_length=300, blank=True)
    occurred_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ['occurred_at']
        indexes = [models.Index(fields=['holding', 'occurred_at'])]


# ═════════════════════════════════════════════════════════════════════════════
# Profit distribution
# ═════════════════════════════════════════════════════════════════════════════

class Distribution(models.Model):
    class Status(models.TextChoices):
        DRAFT = 'draft', 'Awaiting approval'
        POSTED = 'posted', 'Posted'
        SKIPPED = 'skipped', 'Nothing to distribute'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    opportunity = models.ForeignKey(InvestmentOpportunity, on_delete=models.PROTECT,
                                    related_name='distributions')
    period_start = models.DateTimeField()
    period_end = models.DateTimeField()
    status = models.CharField(max_length=8, choices=Status.choices, default=Status.DRAFT,
                              db_index=True)

    revenue = models.DecimalField(max_digits=16, decimal_places=2, default=ZERO)
    cost = models.DecimalField(max_digits=16, decimal_places=2, default=ZERO)
    adjustments_net = models.DecimalField(max_digits=16, decimal_places=2, default=ZERO)
    net_profit = models.DecimalField(max_digits=16, decimal_places=2, default=ZERO)
    units_sold = models.DecimalField(max_digits=14, decimal_places=2, default=ZERO)

    avg_capital = models.DecimalField(max_digits=16, decimal_places=2, default=ZERO)
    funding_ratio = models.DecimalField(max_digits=7, decimal_places=4, default=Decimal('1'))
    effective_share_pct = models.DecimalField(max_digits=7, decimal_places=3, default=ZERO)
    investor_pool = models.DecimalField(max_digits=16, decimal_places=2, default=ZERO)
    company_share = models.DecimalField(max_digits=16, decimal_places=2, default=ZERO)
    yield_pct = models.DecimalField(max_digits=9, decimal_places=4, default=ZERO,
                                    help_text='Investor pool ÷ average capital, for the period.')
    breakdown = models.JSONField(default=dict, blank=True)

    posted_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                  null=True, blank=True, related_name='+')
    posted_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-period_start']
        constraints = [
            models.UniqueConstraint(fields=['opportunity', 'period_start'],
                                    name='uniq_distribution_period'),
        ]

    def __str__(self):
        return f'{self.opportunity} {self.period_start:%Y-%m-%d}'

    @property
    def period_days(self):
        return max(1, (self.period_end - self.period_start).days)


class DistributionAllocation(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    distribution = models.ForeignKey(Distribution, on_delete=models.CASCADE,
                                     related_name='allocations')
    holding = models.ForeignKey(Holding, on_delete=models.PROTECT, related_name='allocations')
    weight_capital = models.DecimalField(max_digits=16, decimal_places=2)
    share_ratio = models.DecimalField(max_digits=9, decimal_places=6)
    amount = models.DecimalField(max_digits=16, decimal_places=2)
    mode_applied = models.CharField(max_length=10, blank=True)

    class Meta:
        ordering = ['-amount']
