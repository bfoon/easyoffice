"""
apps/finance/analytics.py
==========================

The analytical engine for the Financial Control Center. Pure functions — no
HTTP, no templates — that compute every figure the dashboard, reports, and
exports use. Anything financial that has to be *calculated* lives here.

Two accounting bases
--------------------
Cash basis    → revenue when paid (Payment.payment_date),
                 expense when paid (Payment.payment_date)
Accrual basis → revenue when invoiced (IncomingPaymentRequest.issue_date,
                 status != cancelled),
                 expense when committed (Payment OR PaymentRequest.sent
                 OR purchase ceo_approved with actual_cost)

Every public function takes `(start, end, basis='cash')` and returns
plain dicts/lists — easy to JSON-serialize for charts, easy to feed into
PDF/Excel exporters.

Conventions
-----------
- All amounts are Decimal. Callers cast to float at the JSON boundary.
- Dates are date objects (not datetimes).
- "today" means timezone.now().date() unless overridden.
- Currency is a tag on each transaction; this module reports per-currency
  totals AND a "primary currency" rollup using FX rates if you wire them
  in later. For now, primary = GMD.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Iterable, Optional

from django.db.models import Sum, Q, Count, F, Value, DecimalField
from django.db.models.functions import Coalesce, TruncMonth, TruncDay
from django.utils import timezone

from apps.finance.models import (
    Budget,
    Payment,
    PaymentRequest,
    PurchaseRequest,
    EmployeeFinanceRequest,
    EmployeeLoan,
    IncomingPaymentRequest,
    Contract,
)

ZERO = Decimal('0')
PRIMARY_CURRENCY = 'GMD'


# ─── Period helpers ──────────────────────────────────────────────────────────

@dataclass
class Period:
    """A reporting window. Both endpoints inclusive."""
    start: date
    end: date
    label: str = ''

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1


def period_from_preset(preset: str, today: Optional[date] = None) -> Period:
    """
    Build a Period from a string like 'mtd', 'qtd', 'ytd', 'last_30',
    'last_quarter', 'last_year'. Falls back to YTD.
    """
    today = today or timezone.now().date()
    if preset == 'mtd':
        return Period(today.replace(day=1), today, 'Month-to-date')
    if preset == 'qtd':
        q = (today.month - 1) // 3
        return Period(date(today.year, q * 3 + 1, 1), today, 'Quarter-to-date')
    if preset == 'last_30':
        return Period(today - timedelta(days=29), today, 'Last 30 days')
    if preset == 'last_90':
        return Period(today - timedelta(days=89), today, 'Last 90 days')
    if preset == 'last_year':
        return Period(date(today.year - 1, 1, 1), date(today.year - 1, 12, 31), 'Last year')
    # ytd
    return Period(date(today.year, 1, 1), today, 'Year-to-date')


def period_from_request(request, today: Optional[date] = None) -> Period:
    """Pull period from GET params: ?from=YYYY-MM-DD&to=YYYY-MM-DD or ?period=ytd"""
    today = today or timezone.now().date()
    fr = request.GET.get('from')
    to = request.GET.get('to')
    if fr and to:
        try:
            return Period(
                date.fromisoformat(fr),
                date.fromisoformat(to),
                f'{fr} → {to}',
            )
        except ValueError:
            pass
    return period_from_preset(request.GET.get('period', 'ytd'), today)


def basis_from_request(request) -> str:
    b = request.GET.get('basis', 'cash')
    return 'accrual' if b == 'accrual' else 'cash'


# ─── Core revenue / expense aggregators ──────────────────────────────────────

def _payments_qs(period: Period):
    """All outgoing payments inside the period."""
    return Payment.objects.filter(
        payment_date__gte=period.start,
        payment_date__lte=period.end,
    )


def _expenses_cash(period: Period) -> Decimal:
    """Cash basis: sum of all Payment rows in the period (money out)."""
    return _payments_qs(period).aggregate(t=Sum('amount'))['t'] or ZERO


def _expenses_accrual(period: Period) -> Decimal:
    """
    Accrual basis: expense when committed.
    = Payments paid in period
    + PaymentRequests sent in period that aren't paid yet (commitment)
    + PurchaseRequests approved in period with actual_cost set, but no
      payment yet (committed but unpaid)
    Note: we de-duplicate by excluding payment-requests that already have a
    linked_payment (those are already counted in payments).
    """
    paid = _expenses_cash(period)

    committed_unpaid = PaymentRequest.objects.filter(
        status=PaymentRequest.Status.SENT,
        linked_payment__isnull=True,
        created_at__date__gte=period.start,
        created_at__date__lte=period.end,
    ).aggregate(t=Sum('amount'))['t'] or ZERO

    purchase_committed = PurchaseRequest.objects.filter(
        status__in=[
            PurchaseRequest.Status.CEO_APPROVED,
            PurchaseRequest.Status.PROCESSING,
            PurchaseRequest.Status.ORDERED,
        ],
        approval_date__date__gte=period.start,
        approval_date__date__lte=period.end,
        payments__isnull=True,  # not yet paid
    ).aggregate(t=Sum(Coalesce('actual_cost', 'estimated_cost')))['t'] or ZERO

    return paid + committed_unpaid + purchase_committed


def _revenue_cash(period: Period) -> Decimal:
    """
    Cash basis revenue = invoices marked PAID with paid_at inside the period.
    """
    return IncomingPaymentRequest.objects.filter(
        status=IncomingPaymentRequest.Status.PAID,
        paid_at__date__gte=period.start,
        paid_at__date__lte=period.end,
    ).aggregate(
        t=Sum(F('amount') + F('tax_amount') - F('discount_amount'))
    )['t'] or ZERO


def _revenue_accrual(period: Period) -> Decimal:
    """
    Accrual revenue = total of invoices ISSUED in the period whose status is
    anything except cancelled (sent, overdue, paid).
    """
    return IncomingPaymentRequest.objects.filter(
        issue_date__gte=period.start,
        issue_date__lte=period.end,
    ).exclude(
        status=IncomingPaymentRequest.Status.CANCELLED
    ).aggregate(
        t=Sum(F('amount') + F('tax_amount') - F('discount_amount'))
    )['t'] or ZERO


def revenue(period: Period, basis: str = 'cash') -> Decimal:
    return _revenue_accrual(period) if basis == 'accrual' else _revenue_cash(period)


def expenses(period: Period, basis: str = 'cash') -> Decimal:
    return _expenses_accrual(period) if basis == 'accrual' else _expenses_cash(period)


def net_income(period: Period, basis: str = 'cash') -> Decimal:
    return revenue(period, basis) - expenses(period, basis)


# ─── Headline KPIs ───────────────────────────────────────────────────────────

def headline_kpis(period: Period, basis: str = 'cash') -> dict:
    """
    Numbers that go in the dashboard hero strip. All Decimals.
    """
    rev = revenue(period, basis)
    exp = expenses(period, basis)
    net = rev - exp

    # Margin
    margin_pct = (net / rev * 100) if rev else ZERO

    # Burn (expenses per day) and runway
    days = max(period.days, 1)
    daily_burn = exp / days

    # Cash on hand = total revenue collected to date - total payments to date
    cash_in_total = IncomingPaymentRequest.objects.filter(
        status=IncomingPaymentRequest.Status.PAID
    ).aggregate(t=Sum(F('amount') + F('tax_amount') - F('discount_amount')))['t'] or ZERO
    cash_out_total = Payment.objects.aggregate(t=Sum('amount'))['t'] or ZERO
    cash_on_hand = cash_in_total - cash_out_total

    runway_days = (cash_on_hand / daily_burn) if daily_burn > 0 else None

    return {
        'revenue':       rev,
        'expenses':      exp,
        'net_income':    net,
        'margin_pct':    margin_pct,
        'daily_burn':    daily_burn,
        'cash_on_hand':  cash_on_hand,
        'runway_days':   runway_days,
        'period_label':  period.label,
        'period_days':   days,
        'basis':         basis,
    }


# ─── Time series for charts ──────────────────────────────────────────────────

def revenue_vs_expense_series(period: Period, basis: str = 'cash') -> dict:
    """
    Monthly buckets for the period. Returns
        { 'labels': ['Jan', 'Feb', ...],
          'revenue': [Decimal, ...],
          'expenses': [Decimal, ...],
          'net':     [Decimal, ...] }
    """
    # Build month buckets
    months: list[tuple[int, int]] = []
    y, m = period.start.year, period.start.month
    end_y, end_m = period.end.year, period.end.month
    while (y, m) <= (end_y, end_m):
        months.append((y, m))
        m += 1
        if m > 12:
            m = 1
            y += 1

    if basis == 'accrual':
        rev_qs = (
            IncomingPaymentRequest.objects
            .exclude(status=IncomingPaymentRequest.Status.CANCELLED)
            .filter(issue_date__gte=period.start, issue_date__lte=period.end)
            .annotate(bucket=TruncMonth('issue_date'))
            .values('bucket')
            .annotate(total=Sum(F('amount') + F('tax_amount') - F('discount_amount')))
        )
        exp_qs = (
            Payment.objects
            .filter(payment_date__gte=period.start, payment_date__lte=period.end)
            .annotate(bucket=TruncMonth('payment_date'))
            .values('bucket')
            .annotate(total=Sum('amount'))
        )
    else:
        rev_qs = (
            IncomingPaymentRequest.objects
            .filter(
                status=IncomingPaymentRequest.Status.PAID,
                paid_at__date__gte=period.start,
                paid_at__date__lte=period.end,
            )
            .annotate(bucket=TruncMonth('paid_at'))
            .values('bucket')
            .annotate(total=Sum(F('amount') + F('tax_amount') - F('discount_amount')))
        )
        exp_qs = (
            Payment.objects
            .filter(payment_date__gte=period.start, payment_date__lte=period.end)
            .annotate(bucket=TruncMonth('payment_date'))
            .values('bucket')
            .annotate(total=Sum('amount'))
        )

    rev_map = {(r['bucket'].year, r['bucket'].month): r['total'] for r in rev_qs}
    exp_map = {(r['bucket'].year, r['bucket'].month): r['total'] for r in exp_qs}

    labels, rev_series, exp_series, net_series = [], [], [], []
    for y, m in months:
        labels.append(date(y, m, 1).strftime('%b %Y'))
        r = rev_map.get((y, m), ZERO) or ZERO
        e = exp_map.get((y, m), ZERO) or ZERO
        rev_series.append(r)
        exp_series.append(e)
        net_series.append(r - e)

    return {
        'labels':   labels,
        'revenue':  rev_series,
        'expenses': exp_series,
        'net':      net_series,
    }


def cash_flow_daily(period: Period) -> dict:
    """Daily cash in vs cash out — best for shorter periods (≤90 days)."""
    cash_in = (
        IncomingPaymentRequest.objects
        .filter(
            status=IncomingPaymentRequest.Status.PAID,
            paid_at__date__gte=period.start,
            paid_at__date__lte=period.end,
        )
        .annotate(bucket=TruncDay('paid_at'))
        .values('bucket')
        .annotate(total=Sum(F('amount') + F('tax_amount') - F('discount_amount')))
    )
    cash_out = (
        Payment.objects
        .filter(payment_date__gte=period.start, payment_date__lte=period.end)
        .annotate(bucket=TruncDay('payment_date'))
        .values('bucket')
        .annotate(total=Sum('amount'))
    )
    in_map = {r['bucket'].date() if hasattr(r['bucket'], 'date') else r['bucket']: r['total']
              for r in cash_in}
    out_map = {r['bucket']: r['total'] for r in cash_out}

    labels, in_s, out_s, cum = [], [], [], []
    running = ZERO
    d = period.start
    while d <= period.end:
        ci = in_map.get(d, ZERO) or ZERO
        co = out_map.get(d, ZERO) or ZERO
        running += ci - co
        labels.append(d.isoformat())
        in_s.append(ci)
        out_s.append(co)
        cum.append(running)
        d += timedelta(days=1)

    return {
        'labels':     labels,
        'cash_in':    in_s,
        'cash_out':   out_s,
        'cumulative': cum,
    }


# ─── Expense composition ─────────────────────────────────────────────────────

def expense_by_type(period: Period) -> list[dict]:
    """
    Cash-basis breakdown of payments by payment_type. Used for pie/donut.
    """
    rows = (
        _payments_qs(period)
        .values('payment_type')
        .annotate(total=Sum('amount'))
        .order_by('-total')
    )
    type_labels = dict(Payment.PaymentType.choices)
    return [
        {
            'key':    r['payment_type'],
            'label':  type_labels.get(r['payment_type'], r['payment_type']),
            'amount': r['total'] or ZERO,
        }
        for r in rows
    ]


def expense_by_department(period: Period) -> list[dict]:
    """
    Spend per department, derived via the budget link on Payment, falling
    back to PurchaseRequest.department for purchase-related payments.
    """
    rows = (
        _payments_qs(period)
        .values('budget__department__name')
        .annotate(total=Sum('amount'))
        .order_by('-total')
    )
    return [
        {
            'department': r['budget__department__name'] or 'Unallocated',
            'amount':     r['total'] or ZERO,
        }
        for r in rows
    ]


def revenue_by_customer(period: Period, basis: str = 'cash', limit: int = 10) -> list[dict]:
    """Top N customers by revenue for the period."""
    if basis == 'accrual':
        qs = (
            IncomingPaymentRequest.objects
            .exclude(status=IncomingPaymentRequest.Status.CANCELLED)
            .filter(issue_date__gte=period.start, issue_date__lte=period.end)
        )
    else:
        qs = (
            IncomingPaymentRequest.objects
            .filter(
                status=IncomingPaymentRequest.Status.PAID,
                paid_at__date__gte=period.start,
                paid_at__date__lte=period.end,
            )
        )
    rows = (
        qs.values('customer_name')
        .annotate(total=Sum(F('amount') + F('tax_amount') - F('discount_amount')))
        .order_by('-total')[:limit]
    )
    return [
        {'customer': r['customer_name'] or '—', 'amount': r['total'] or ZERO}
        for r in rows
    ]


# ─── Budget vs Actual ────────────────────────────────────────────────────────

def budget_vs_actual(fiscal_year: Optional[int] = None) -> dict:
    """
    Per-budget snapshot: total, spent, balance, utilization, status.
    """
    fiscal_year = fiscal_year or timezone.now().year
    qs = (
        Budget.objects
        .filter(fiscal_year=fiscal_year)
        .select_related('department', 'unit')
        .order_by('-total_amount')
    )

    rows = []
    grand_total = grand_spent = ZERO
    for b in qs:
        total = b.total_amount or ZERO
        spent = b.spent_amount or ZERO
        balance = total - spent
        util = (spent / total * 100) if total else ZERO
        rows.append({
            'id':           str(b.id),
            'name':         b.name,
            'department':   b.department.name if b.department else '—',
            'unit':         b.unit.name if b.unit else '—',
            'status':       b.status,
            'total':        total,
            'spent':        spent,
            'balance':      balance,
            'utilization':  util,
            'over_budget':  spent > total,
        })
        grand_total += total
        grand_spent += spent

    return {
        'fiscal_year':  fiscal_year,
        'rows':         rows,
        'grand_total':  grand_total,
        'grand_spent':  grand_spent,
        'grand_balance': grand_total - grand_spent,
        'grand_utilization': (grand_spent / grand_total * 100) if grand_total else ZERO,
    }


# ─── Ageing analysis ─────────────────────────────────────────────────────────

AGEING_BUCKETS = [
    ('current', 'Current', 0,   0),
    ('1_30',   '1–30 days',  1,  30),
    ('31_60',  '31–60 days', 31, 60),
    ('61_90',  '61–90 days', 61, 90),
    ('over_90', '90+ days',  91, 99999),
]


def receivables_ageing(today: Optional[date] = None) -> dict:
    """
    AR ageing — open invoices grouped by how long they've been overdue
    (or by days until due if not yet due).

    Bucket logic:
    - 'current' = unpaid + due_date >= today (or due_date null = treated as current)
    - others    = unpaid + days since due_date in range
    """
    today = today or timezone.now().date()
    open_invoices = IncomingPaymentRequest.objects.filter(
        status__in=[
            IncomingPaymentRequest.Status.SENT,
            IncomingPaymentRequest.Status.OVERDUE,
        ],
    ).select_related('project')

    buckets = {key: {'label': lbl, 'amount': ZERO, 'count': 0, 'invoices': []}
               for key, lbl, *_ in AGEING_BUCKETS}
    total = ZERO

    for inv in open_invoices:
        amt = (inv.amount or ZERO) + (inv.tax_amount or ZERO) - (inv.discount_amount or ZERO)
        total += amt
        bucket = 'current'
        if inv.due_date and inv.due_date < today:
            days = (today - inv.due_date).days
            for key, _, lo, hi in AGEING_BUCKETS:
                if key == 'current':
                    continue
                if lo <= days <= hi:
                    bucket = key
                    break
        b = buckets[bucket]
        b['amount'] += amt
        b['count'] += 1
        if len(b['invoices']) < 5:
            b['invoices'].append({
                'id':           str(inv.id),
                'invoice_number': inv.invoice_number,
                'customer':     inv.customer_name,
                'amount':       amt,
                'due_date':     inv.due_date,
                'days_overdue': inv.days_overdue,
            })

    return {
        'as_of':  today,
        'total':  total,
        'buckets': [
            {**buckets[key], 'key': key}
            for key, *_ in AGEING_BUCKETS
        ],
    }


def payables_ageing(today: Optional[date] = None) -> dict:
    """
    AP ageing — outgoing PaymentRequests that are SENT but unpaid.
    """
    today = today or timezone.now().date()
    open_pr = PaymentRequest.objects.filter(
        status=PaymentRequest.Status.SENT,
        linked_payment__isnull=True,
    )

    buckets = {key: {'label': lbl, 'amount': ZERO, 'count': 0, 'items': []}
               for key, lbl, *_ in AGEING_BUCKETS}
    total = ZERO

    for pr in open_pr:
        amt = pr.amount or ZERO
        total += amt
        bucket = 'current'
        if pr.due_date and pr.due_date < today:
            days = (today - pr.due_date).days
            for key, _, lo, hi in AGEING_BUCKETS:
                if key == 'current':
                    continue
                if lo <= days <= hi:
                    bucket = key
                    break
        b = buckets[bucket]
        b['amount'] += amt
        b['count'] += 1
        if len(b['items']) < 5:
            b['items'].append({
                'id':       str(pr.id),
                'title':    pr.title,
                'recipient': pr.effective_recipient_name,
                'amount':   amt,
                'due_date': pr.due_date,
            })

    return {
        'as_of':  today,
        'total':  total,
        'buckets': [
            {**buckets[key], 'key': key}
            for key, *_ in AGEING_BUCKETS
        ],
    }


# ─── P&L (Income Statement) ──────────────────────────────────────────────────

def income_statement(period: Period, basis: str = 'cash') -> dict:
    """
    Structured P&L. Returns a section list ready for both UI and PDF/Excel.
    """
    rev = revenue(period, basis)

    # Expenses, broken down by Payment.PaymentType
    if basis == 'cash':
        rows = list(_payments_qs(period).values('payment_type').annotate(total=Sum('amount')))
    else:
        # Accrual = paid + committed-but-unpaid; we still want the type split,
        # so we union the two sources by type.
        paid = list(_payments_qs(period).values('payment_type').annotate(total=Sum('amount')))
        # Committed payment requests don't have payment_type? They do.
        committed = list(
            PaymentRequest.objects.filter(
                status=PaymentRequest.Status.SENT,
                linked_payment__isnull=True,
                created_at__date__gte=period.start,
                created_at__date__lte=period.end,
            ).values('payment_type').annotate(total=Sum('amount'))
        )
        merged = defaultdict(lambda: ZERO)
        for r in paid + committed:
            merged[r['payment_type']] += r['total'] or ZERO
        rows = [{'payment_type': k, 'total': v} for k, v in merged.items()]

    type_labels = dict(Payment.PaymentType.choices)
    expense_lines = sorted(
        [
            {
                'label':  type_labels.get(r['payment_type'], r['payment_type']),
                'amount': r['total'] or ZERO,
            }
            for r in rows
        ],
        key=lambda x: x['amount'],
        reverse=True,
    )
    total_expenses = sum((r['amount'] for r in expense_lines), ZERO)
    net = rev - total_expenses

    return {
        'period':       period,
        'basis':        basis,
        'revenue':      rev,
        'revenue_lines': [{'label': 'Customer Invoices', 'amount': rev}],
        'expense_lines': expense_lines,
        'total_expenses': total_expenses,
        'net_income':   net,
        'margin_pct':   (net / rev * 100) if rev else ZERO,
    }


# ─── Cash flow statement ─────────────────────────────────────────────────────

def cash_flow_statement(period: Period) -> dict:
    """
    Simplified statement of cash flows — operating activities only, since
    EasyOffice doesn't track investing/financing yet.
    """
    cash_in = IncomingPaymentRequest.objects.filter(
        status=IncomingPaymentRequest.Status.PAID,
        paid_at__date__gte=period.start,
        paid_at__date__lte=period.end,
    ).aggregate(t=Sum(F('amount') + F('tax_amount') - F('discount_amount')))['t'] or ZERO

    # Outflows by direction
    out_qs = _payments_qs(period)
    out_by_dir = out_qs.values('direction').annotate(t=Sum('amount'))
    dir_labels = dict(Payment.Direction.choices)
    outflows = [
        {'label': dir_labels.get(r['direction'], r['direction']), 'amount': r['t'] or ZERO}
        for r in out_by_dir
    ]
    total_out = sum((r['amount'] for r in outflows), ZERO)
    net = cash_in - total_out

    # Opening / closing cash position (cumulative across all time, snapshotted)
    opening_in = IncomingPaymentRequest.objects.filter(
        status=IncomingPaymentRequest.Status.PAID,
        paid_at__date__lt=period.start,
    ).aggregate(t=Sum(F('amount') + F('tax_amount') - F('discount_amount')))['t'] or ZERO
    opening_out = Payment.objects.filter(
        payment_date__lt=period.start,
    ).aggregate(t=Sum('amount'))['t'] or ZERO
    opening_cash = opening_in - opening_out
    closing_cash = opening_cash + net

    return {
        'period':       period,
        'opening_cash': opening_cash,
        'cash_in':      cash_in,
        'inflows':      [{'label': 'Customer collections', 'amount': cash_in}],
        'outflows':     outflows,
        'total_outflows': total_out,
        'net_cash_change': net,
        'closing_cash': closing_cash,
    }


# ─── Comparison helpers (for "vs prior period" badges) ───────────────────────

def previous_period(period: Period) -> Period:
    """Return the same-length window immediately before this one."""
    span = period.days
    new_end = period.start - timedelta(days=1)
    new_start = new_end - timedelta(days=span - 1)
    return Period(new_start, new_end, f'Previous {period.label or ""}'.strip())


def pct_change(current: Decimal, previous: Decimal) -> Optional[Decimal]:
    """Returns None if previous is zero (avoids div-by-zero / infinity badges)."""
    if previous == 0:
        return None
    return (current - previous) / previous * 100


# ─── Contract & loan exposure ────────────────────────────────────────────────

def contract_exposure() -> dict:
    """
    Total committed cost of all active contracts — useful for runway/burn
    forecasting.
    """
    active = Contract.objects.filter(
        status__in=[Contract.Status.ACTIVE, Contract.Status.EXPIRING]
    )
    monthly_committed = ZERO
    for c in active:
        if c.billing_cycle == Contract.BillingCycle.MONTHLY:
            monthly_committed += c.standard_cost or ZERO
        elif c.billing_cycle == Contract.BillingCycle.YEARLY:
            monthly_committed += (c.standard_cost or ZERO) / 12
        elif c.billing_cycle == Contract.BillingCycle.QUARTERLY:
            monthly_committed += (c.standard_cost or ZERO) / 3
        elif c.billing_cycle == Contract.BillingCycle.WEEKLY:
            monthly_committed += (c.standard_cost or ZERO) * Decimal('4.33')

    return {
        'active_count': active.count(),
        'expiring_count': active.filter(status=Contract.Status.EXPIRING).count(),
        'monthly_committed': monthly_committed,
        'annual_committed':  monthly_committed * 12,
    }


def loan_exposure() -> dict:
    """Outstanding employee loans."""
    active = EmployeeLoan.objects.filter(status=EmployeeLoan.Status.ACTIVE)
    disbursed = active.aggregate(t=Sum('disbursed_amount'))['t'] or ZERO
    repaid    = active.aggregate(t=Sum('amount_repaid'))['t'] or ZERO
    return {
        'active_count':  active.count(),
        'disbursed':     disbursed,
        'repaid':        repaid,
        'outstanding':   disbursed - repaid,
    }
