"""
apps/investments/services/forecast.py
─────────────────────────────────────
Return estimates, projections and chart series.

Rates are expressed as a DAILY rate (profit ÷ capital ÷ days):

  • Listing with posted payouts → mean and spread of the per-period yields
    (last 12 periods), converted to a daily rate.
  • Listing with no payouts yet → "estimated" from the last 90 days of
    sales of its items: profit × investor share ÷ target capital ÷ 90.

Projections give three lines — cautious (mean − 1 spread, never below 0),
expected, strong (mean + 1 spread) — and respect the holding's payout mode:
reinvest compounds daily, wallet payouts stay simple.
"""
from __future__ import annotations

import math
from collections import OrderedDict
from datetime import timedelta
from decimal import Decimal

from django.db.models import Sum
from django.utils import timezone

ZERO = Decimal('0.00')


def _f(d):
    return float(d or 0)


# ─────────────────────────────────────────────────────────────────────────────
# Rates
# ─────────────────────────────────────────────────────────────────────────────

def opportunity_rate(opportunity, periods=12) -> dict:
    from apps.investments.models import Distribution
    dists = list(opportunity.distributions
                 .filter(status__in=[Distribution.Status.POSTED, Distribution.Status.SKIPPED],
                         avg_capital__gt=0)
                 .order_by('-period_start')[:periods])
    if dists:
        daily = [_f(d.yield_pct) / 100 / d.period_days for d in dists]
        mean = sum(daily) / len(daily)
        spread = (math.sqrt(sum((x - mean) ** 2 for x in daily) / (len(daily) - 1))
                  if len(daily) > 1 else mean * 0.35)
        return {'basis': 'actual', 'periods': len(dists), 'daily': mean,
                'low': max(0.0, mean - spread), 'high': mean + spread,
                'series': [{'label': timezone.localtime(d.period_start).strftime('%d %b'),
                            'yield': round(_f(d.yield_pct), 3)} for d in reversed(dists)]}

    from apps.investments.services.profit import trailing_profit
    target = _f(opportunity.target_capital)
    if target <= 0:
        return {'basis': 'none', 'periods': 0, 'daily': 0.0, 'low': 0.0, 'high': 0.0, 'series': []}
    tp = trailing_profit(opportunity, 90)
    pool = max(0.0, _f(tp['net_profit'])) * _f(opportunity.investor_share_pct) / 100
    daily = pool / target / 90
    return {'basis': 'estimated', 'periods': 0, 'daily': daily,
            'low': daily * 0.6, 'high': daily * 1.3, 'series': [],
            'trailing_profit': _f(tp['net_profit']), 'trailing_units': _f(tp['units_sold'])}


def annual_pct(daily, compound=True):
    if compound:
        return ((1 + daily) ** 365 - 1) * 100
    return daily * 365 * 100


# ─────────────────────────────────────────────────────────────────────────────
# Projections
# ─────────────────────────────────────────────────────────────────────────────

def _project(capital, daily, days, compound):
    if compound:
        end_cap = capital * (1 + daily) ** days
        return end_cap, end_cap - capital
    return capital, capital * daily * days


def project(capital: float, rate: dict, months=12, compound=True) -> dict:
    """Monthly points for the three scenarios."""
    labels, out = [], {'low': [], 'base': [], 'high': []}
    today = timezone.localdate()
    for m in range(0, months + 1):
        days = int(round(m * 30.437))
        labels.append((today + timedelta(days=days)).strftime('%b %y'))
        for key, r in (('low', rate['low']), ('base', rate['daily']), ('high', rate['high'])):
            cap, prof = _project(capital, r, days, compound)
            out[key].append({'capital': round(cap, 2), 'profit': round(prof, 2),
                             'value': round(cap + (0 if compound else prof), 2)})
    return {'labels': labels, **out}


def simulate(amount: float, rate: dict, months: int, compound: bool) -> dict:
    days = int(round(months * 30.437))
    res = {}
    for key, r in (('low', rate['low']), ('base', rate['daily']), ('high', rate['high'])):
        cap, prof = _project(amount, r, days, compound)
        res[key] = round(prof, 2)
    return res


def portfolio_forecast(account, months=12) -> dict:
    """Sum of per-holding projections (each with its own rate and payout mode)."""
    from apps.investments.models import Holding
    holdings = list(account.holdings.filter(status=Holding.Status.ACTIVE, capital__gt=0)
                    .select_related('opportunity'))
    labels, totals = None, None
    per_holding = []
    for h in holdings:
        rate = opportunity_rate(h.opportunity)
        p = project(_f(h.capital), rate, months, compound=(h.payout_mode == 'reinvest'))
        labels = p['labels']
        if totals is None:
            totals = {k: [{'capital': 0, 'profit': 0} for _ in p['base']] for k in ('low', 'base', 'high')}
        for k in ('low', 'base', 'high'):
            for i, pt in enumerate(p[k]):
                totals[k][i]['capital'] += pt['capital']
                totals[k][i]['profit'] += pt['profit']
        per_holding.append({
            'holding': h, 'rate': rate,
            'annual_pct': annual_pct(rate['daily'], h.payout_mode == 'reinvest'),
            'profit_12m': p['base'][-1]['profit'],
        })
    if totals is None:
        today = timezone.localdate()
        labels = [(today + timedelta(days=int(m * 30.437))).strftime('%b %y') for m in range(months + 1)]
        totals = {k: [{'capital': 0, 'profit': 0} for _ in labels] for k in ('low', 'base', 'high')}
    for k in totals:
        for pt in totals[k]:
            pt['capital'] = round(pt['capital'], 2)
            pt['profit'] = round(pt['profit'], 2)
    return {'labels': labels, 'totals': totals, 'holdings': per_holding}


# ─────────────────────────────────────────────────────────────────────────────
# History series
# ─────────────────────────────────────────────────────────────────────────────

def portfolio_history(account, days=365) -> dict:
    """Daily capital and cumulative profit since first investment (≤ `days`)."""
    from apps.investments.models import HoldingEvent
    events = list(HoldingEvent.objects.filter(holding__account=account).order_by('occurred_at')
                  .values('occurred_at', 'kind', 'amount', 'capital_delta'))
    if not events:
        return {'labels': [], 'capital': [], 'profit': [], 'principal': []}

    today = timezone.localdate()
    first = timezone.localtime(events[0]['occurred_at']).date()
    start = max(first, today - timedelta(days=days))

    capital = principal = profit = Decimal('0')
    by_day = OrderedDict()
    idx = 0
    # roll forward everything before the window
    while idx < len(events) and timezone.localtime(events[idx]['occurred_at']).date() < start:
        capital, principal, profit = _apply(events[idx], capital, principal, profit)
        idx += 1
    d = start
    while d <= today:
        while idx < len(events) and timezone.localtime(events[idx]['occurred_at']).date() <= d:
            capital, principal, profit = _apply(events[idx], capital, principal, profit)
            idx += 1
        by_day[d] = (capital, principal, profit)
        d += timedelta(days=1)

    step = max(1, len(by_day) // 120)   # keep charts light
    items = list(by_day.items())
    sampled = items[::step]
    if items and sampled[-1] != items[-1]:
        sampled.append(items[-1])
    return {
        'labels': [k.strftime('%d %b %y') for k, _ in sampled],
        'capital': [_f(v[0]) for _, v in sampled],
        'principal': [_f(v[1]) for _, v in sampled],
        'profit': [_f(v[2]) for _, v in sampled],
    }


def _apply(ev, capital, principal, profit):
    kind, amt, delta = ev['kind'], ev['amount'], ev['capital_delta']
    capital += delta
    if kind == 'invest':
        principal += amt
    elif kind == 'redeem':
        principal -= min(principal, amt)
    elif kind in ('profit_reinvest', 'profit_paid'):
        profit += amt
    elif kind in ('loss', 'fee'):
        profit -= amt
    return capital, principal, profit


def aum_history(days=180) -> dict:
    """Total capital under management per day (admin dashboard)."""
    from apps.investments.models import HoldingEvent
    today = timezone.localdate()
    start = today - timedelta(days=days)
    base = HoldingEvent.objects.filter(occurred_at__date__lt=start).aggregate(
        s=Sum('capital_delta'))['s'] or ZERO
    rows = (HoldingEvent.objects.filter(occurred_at__date__gte=start)
            .values_list('occurred_at', 'capital_delta').order_by('occurred_at'))
    deltas = {}
    for at, delta in rows:
        dd = timezone.localtime(at).date()
        deltas[dd] = deltas.get(dd, ZERO) + delta
    labels, values, running = [], [], base
    d = start
    while d <= today:
        running += deltas.get(d, ZERO)
        labels.append(d.strftime('%d %b'))
        values.append(_f(running))
        d += timedelta(days=1)
    step = max(1, len(labels) // 90)
    if step > 1:
        keep = list(range(0, len(labels), step))
        if keep[-1] != len(labels) - 1:
            keep.append(len(labels) - 1)
        labels = [labels[i] for i in keep]
        values = [values[i] for i in keep]
    return {'labels': labels, 'values': values}
