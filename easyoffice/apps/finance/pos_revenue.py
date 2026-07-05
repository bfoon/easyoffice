"""
apps/finance/pos_revenue.py
───────────────────────────
Bridges POS takings into the Financial Control Center.

The analytics module sums revenue directly from source tables (invoices +
legacy IncomingPaymentRequest). POS is a third income source, added here
in the SAME defensive style: a lazy import wrapped in try/except, so if
the POS app isn't installed these helpers return zero and analytics keep
working unchanged.

Money semantics — matched to the rest of analytics.py:

  • Only COMPLETED sales count. Voided sales never contribute.
  • Cash AND accrual basis are the SAME date for POS: a walk-in sale is
    invoiced and paid at the same instant (completed_at). There's no
    receivable lag, so both bases use completed_at. This is correct
    double-entry: the sale is simultaneously earned and collected.
  • sale.total already includes tax and nets the discount, so no extra
    arithmetic is needed (unlike the IncomingPaymentRequest amount +
    tax − discount expansion).

Every function takes the analytics Period and returns Decimal / dicts
shaped exactly like the existing aggregators, so they slot into the
headline KPIs, the revenue-vs-expense series, the daily cash-flow chart,
and the revenue-by-customer table without reshaping.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from django.db.models import Sum
from django.db.models.functions import TruncMonth, TruncDay

ZERO = Decimal('0.00')


def _completed_sales_qs(period):
    """
    COMPLETED POS sales whose completed_at falls inside the period, or an
    empty queryset if the POS app isn't available. Same date field is used
    for both cash and accrual basis (walk-in = earned and collected at once).
    """
    try:
        from apps.pos.models import POSSale
    except Exception:
        return None

    return POSSale.objects.filter(
        status=POSSale.Status.COMPLETED,
        completed_at__date__gte=period.start,
        completed_at__date__lte=period.end,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Totals
# ─────────────────────────────────────────────────────────────────────────────

def pos_revenue(period, basis: str = 'cash') -> Decimal:
    """Total POS takings in the period. Same for cash and accrual basis."""
    qs = _completed_sales_qs(period)
    if qs is None:
        return ZERO
    return qs.aggregate(t=Sum('total'))['t'] or ZERO


# ─────────────────────────────────────────────────────────────────────────────
# Monthly series (feeds revenue_vs_expense_series)
# ─────────────────────────────────────────────────────────────────────────────

def pos_revenue_by_month(period) -> dict:
    """
    { (year, month): Decimal } for completed POS sales, so the caller can
    add it onto the invoice-based monthly revenue map.
    """
    qs = _completed_sales_qs(period)
    if qs is None:
        return {}
    rows = (qs.annotate(bucket=TruncMonth('completed_at'))
              .values('bucket')
              .annotate(total=Sum('total')))
    out = {}
    for r in rows:
        b = r['bucket']
        out[(b.year, b.month)] = (out.get((b.year, b.month)) or ZERO) + (r['total'] or ZERO)
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Daily series (feeds cash_flow_daily)
# ─────────────────────────────────────────────────────────────────────────────

def pos_revenue_by_day(period) -> dict:
    """{ date: Decimal } of POS takings per day — cash IN for the cash-flow chart."""
    qs = _completed_sales_qs(period)
    if qs is None:
        return {}
    rows = (qs.annotate(bucket=TruncDay('completed_at'))
              .values('bucket')
              .annotate(total=Sum('total')))
    out = {}
    for r in rows:
        day = r['bucket'].date() if hasattr(r['bucket'], 'date') else r['bucket']
        out[day] = (out.get(day) or ZERO) + (r['total'] or ZERO)
    return out


# ─────────────────────────────────────────────────────────────────────────────
# By customer (feeds revenue_by_customer)
# ─────────────────────────────────────────────────────────────────────────────

def pos_revenue_by_customer(period) -> list[dict]:
    """
    POS takings grouped by customer label. Walk-ins with no captured name
    are grouped under 'POS walk-in'. Returns [{'name': str, 'amount': Decimal}].
    """
    qs = _completed_sales_qs(period)
    if qs is None:
        return []

    named = (qs.exclude(customer_name='')
               .values('customer_name')
               .annotate(total=Sum('total')))
    result = [{'name': r['customer_name'], 'amount': r['total'] or ZERO}
              for r in named]

    walkin_total = (qs.filter(customer_name='')
                      .aggregate(t=Sum('total'))['t'] or ZERO)
    if walkin_total:
        result.append({'name': 'POS walk-in', 'amount': walkin_total})

    return result


# ─────────────────────────────────────────────────────────────────────────────
# Payment-method split (for the POS card on the dashboard / Z-reconciliation)
# ─────────────────────────────────────────────────────────────────────────────

def pos_revenue_by_method(period) -> list[dict]:
    """[{'method': 'cash'|'card'|…, 'amount': Decimal, 'count': int}]."""
    qs = _completed_sales_qs(period)
    if qs is None:
        return []
    from django.db.models import Count
    rows = (qs.values('payment_method')
              .annotate(amount=Sum('total'), count=Count('id'))
              .order_by('-amount'))
    return [{'method': r['payment_method'],
             'amount': r['amount'] or ZERO,
             'count': r['count']} for r in rows]


def pos_summary(period) -> dict:
    """One-call summary for a dashboard card."""
    qs = _completed_sales_qs(period)
    if qs is None:
        return {'available': False, 'total': ZERO, 'count': 0, 'by_method': []}
    from django.db.models import Count
    agg = qs.aggregate(total=Sum('total'), count=Count('id'))
    return {
        'available': True,
        'total': agg['total'] or ZERO,
        'count': agg['count'] or 0,
        'by_method': pos_revenue_by_method(period),
    }
