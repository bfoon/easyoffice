"""
apps/investments/services/distribution.py
─────────────────────────────────────────
Turns period profit into investor payouts.

    pool         = net_profit × investor_share_pct × funding_ratio
    funding_ratio = min(1, time-weighted capital ÷ target)   (if scale_by_funding)
    each holding  = pool × (its time-weighted capital ÷ total time-weighted capital)

Time weighting means money invested on the 28th of a monthly period earns
about 3/30 of what the same money earns over the full month — topping up just
before payout gains nothing, and investing "any time" stays fair.

Losses: with loss_policy=absorb a loss-making period pays nothing; with
loss_policy=share the (negative) pool reduces capital, never below zero.

Periods are aligned to local calendar boundaries (day / ISO week / month) and
Distribution is unique per (opportunity, period_start): running the job twice
cannot pay twice.
"""
from __future__ import annotations

import logging
from datetime import datetime, time, timedelta
from decimal import ROUND_DOWN, Decimal

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.investments.services.ledger import CENT, ZERO, _event, _locked_account, _post

logger = logging.getLogger(__name__)

MAX_BACKFILL_PERIODS = 400


# ─────────────────────────────────────────────────────────────────────────────
# Periods
# ─────────────────────────────────────────────────────────────────────────────

def _aware(d):
    return timezone.make_aware(datetime.combine(d, time.min))


def period_containing(freq: str, moment) -> tuple:
    local = timezone.localtime(moment).date()
    if freq == 'daily':
        start = local
        end = start + timedelta(days=1)
    elif freq == 'weekly':
        start = local - timedelta(days=local.weekday())
        end = start + timedelta(days=7)
    else:
        start = local.replace(day=1)
        end = (start.replace(year=start.year + 1, month=1) if start.month == 12
               else start.replace(month=start.month + 1))
    return _aware(start), _aware(end)


def next_period(freq: str, period_end) -> tuple:
    return period_containing(freq, period_end)


def next_payout_date(opportunity, now=None):
    now = now or timezone.now()
    _, end = period_containing(opportunity.distribution_frequency, now)
    return end


# ─────────────────────────────────────────────────────────────────────────────
# Time-weighted capital
# ─────────────────────────────────────────────────────────────────────────────

def capital_weight(holding, start, end) -> Decimal:
    """Average capital held over [start, end)."""
    total_seconds = Decimal((end - start).total_seconds())
    if total_seconds <= 0:
        return ZERO
    before = (holding.events.filter(occurred_at__lt=start)
              .order_by('-occurred_at').values_list('capital_after', flat=True).first())
    capital = before if before is not None else ZERO
    cursor = start
    area = Decimal('0')
    for ev in holding.events.filter(occurred_at__gte=start, occurred_at__lt=end).order_by('occurred_at'):
        area += capital * Decimal((ev.occurred_at - cursor).total_seconds())
        capital = ev.capital_after
        cursor = ev.occurred_at
    area += capital * Decimal((end - cursor).total_seconds())
    return (area / total_seconds).quantize(CENT)


def _split(pool: Decimal, weights: list) -> list:
    """Split pool by weights to the cent; leftover cents go to the largest weight."""
    total = sum(weights) or Decimal('0')
    if total == 0 or pool == 0:
        return [ZERO for _ in weights]
    shares = [(pool * w / total).quantize(CENT, rounding=ROUND_DOWN) for w in weights]
    leftover = (pool - sum(shares)).quantize(CENT)
    if leftover and shares:
        idx = max(range(len(weights)), key=lambda i: weights[i])
        shares[idx] += leftover
    return shares


# ─────────────────────────────────────────────────────────────────────────────
# Compute (no writes)
# ─────────────────────────────────────────────────────────────────────────────

def compute_distribution(opportunity, start, end) -> dict:
    from apps.investments.models import InvestmentOpportunity
    from apps.investments.services.profit import compute_profit

    profit = compute_profit(opportunity, start, end)

    holdings = list(opportunity.holdings.filter(first_invested_at__lt=end)
                    .select_related('account__user'))
    weights = [capital_weight(h, start, end) for h in holdings]
    pairs = [(h, w) for h, w in zip(holdings, weights) if w > 0]
    avg_capital = sum((w for _, w in pairs), ZERO)

    funding_ratio = Decimal('1')
    if opportunity.scale_by_funding and opportunity.target_capital > 0:
        funding_ratio = min(Decimal('1'), avg_capital / opportunity.target_capital)
    funding_ratio = funding_ratio.quantize(Decimal('0.0001'))
    eff_share = (opportunity.investor_share_pct * funding_ratio).quantize(Decimal('0.001'))

    net = profit['net_profit']
    pool = ZERO
    if avg_capital > 0:
        if net > 0:
            pool = (net * eff_share / 100).quantize(CENT, rounding=ROUND_DOWN)
        elif net < 0 and opportunity.loss_policy == InvestmentOpportunity.LossPolicy.SHARE:
            pool = (net * eff_share / 100).quantize(CENT)

    amounts = _split(pool, [w for _, w in pairs])
    allocations = []
    for (h, w), amt in zip(pairs, amounts):
        allocations.append({
            'holding': h,
            'weight': w,
            'ratio': (w / avg_capital).quantize(Decimal('0.000001')) if avg_capital else Decimal('0'),
            'amount': amt,
        })

    yield_pct = (pool / avg_capital * 100).quantize(Decimal('0.0001')) if avg_capital else Decimal('0')
    return {
        **profit,
        'period_start': start, 'period_end': end,
        'avg_capital': avg_capital, 'funding_ratio': funding_ratio,
        'effective_share_pct': eff_share, 'investor_pool': pool,
        'company_share': (net - pool).quantize(CENT),
        'yield_pct': yield_pct, 'allocations': allocations,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Persist / post
# ─────────────────────────────────────────────────────────────────────────────

def create_distribution(opportunity, start, end, *, actor=None, auto_post=None):
    """Create the Distribution for a period (idempotent). Returns (dist, created)."""
    from apps.investments.models import Distribution, DistributionAllocation

    existing = Distribution.objects.filter(opportunity=opportunity, period_start=start).first()
    if existing:
        return existing, False

    data = compute_distribution(opportunity, start, end)
    status = Distribution.Status.DRAFT
    if data['investor_pool'] == ZERO:
        status = Distribution.Status.SKIPPED

    try:
        with transaction.atomic():
            dist = Distribution.objects.create(
                opportunity=opportunity, period_start=start, period_end=end, status=status,
                revenue=data['revenue'], cost=data['cost'],
                adjustments_net=data['adjustments_net'], net_profit=data['net_profit'],
                units_sold=data['units_sold'], avg_capital=data['avg_capital'],
                funding_ratio=data['funding_ratio'],
                effective_share_pct=data['effective_share_pct'],
                investor_pool=data['investor_pool'], company_share=data['company_share'],
                yield_pct=data['yield_pct'], breakdown=data['breakdown'],
            )
            for a in data['allocations']:
                DistributionAllocation.objects.create(
                    distribution=dist, holding=a['holding'], weight_capital=a['weight'],
                    share_ratio=a['ratio'], amount=a['amount'],
                )
    except IntegrityError:
        return Distribution.objects.get(opportunity=opportunity, period_start=start), False

    should_post = opportunity.auto_post if auto_post is None else auto_post
    if status == Distribution.Status.DRAFT and should_post:
        post_distribution(dist, actor=actor)
    return dist, True


def post_distribution(distribution, *, actor=None):
    """Apply a draft distribution to holdings / wallets."""
    from apps.investments.models import (Distribution, Holding, HoldingEvent,
                                         WalletTransaction)

    with transaction.atomic():
        dist = Distribution.objects.select_for_update().select_related('opportunity').get(pk=distribution.pk)
        if dist.status != Distribution.Status.DRAFT:
            raise ValueError('Only distributions awaiting approval can be posted.')
        opp = dist.opportunity
        label = f'{opp.title} · {timezone.localtime(dist.period_start):%d %b}' \
                f'–{timezone.localtime(dist.period_end - timedelta(seconds=1)):%d %b %Y}'

        for alloc in dist.allocations.select_related('holding'):
            h = Holding.objects.select_for_update().get(pk=alloc.holding_id)
            amt = alloc.amount
            if amt > 0:
                h.profit_total += amt
                if h.payout_mode == Holding.PayoutMode.REINVEST and h.status == Holding.Status.ACTIVE:
                    _event(h, HoldingEvent.Kind.PROFIT_REINVEST, amt, amt,
                           distribution=dist, note=f'Profit reinvested — {label}')
                    h.profit_reinvested += amt
                    alloc.mode_applied = 'reinvest'
                else:
                    acc = _locked_account(h.account_id)
                    txn = _post(acc, WalletTransaction.Kind.PROFIT, amt,
                                description=f'Profit — {label}', holding=h, actor=actor)
                    _event(h, HoldingEvent.Kind.PROFIT_PAID, amt, ZERO, distribution=dist,
                           wallet_txn=txn, note=f'Profit paid to wallet — {label}')
                    h.profit_paid_out += amt
                    alloc.mode_applied = 'wallet'
            elif amt < 0:
                loss = min(h.capital, -amt)
                if loss > 0:
                    _event(h, HoldingEvent.Kind.LOSS, loss, -loss, distribution=dist,
                           note=f'Share of loss — {label}')
                    h.losses_total += loss
                    if h.capital == ZERO:
                        h.status = Holding.Status.CLOSED
                alloc.mode_applied = 'loss'
            h.save()
            alloc.save(update_fields=['mode_applied'])

        dist.status = Distribution.Status.POSTED
        dist.posted_by = actor if getattr(actor, 'pk', None) else None
        dist.posted_at = timezone.now()
        dist.save(update_fields=['status', 'posted_by', 'posted_at'])

    try:
        from apps.investments.services.notify import distribution_posted
        transaction.on_commit(lambda: distribution_posted(dist))
    except Exception:
        logger.exception('investments: distribution notification failed')
    return dist


def due_periods(opportunity, now=None) -> list:
    """Completed periods that have no Distribution yet."""
    from django.db.models import Max, Min
    now = now or timezone.now()
    freq = opportunity.distribution_frequency

    last_end = opportunity.distributions.aggregate(m=Max('period_end'))['m']
    if last_end:
        start, end = next_period(freq, last_end)
    else:
        first = opportunity.holdings.aggregate(m=Min('first_invested_at'))['m']
        if not first:
            return []
        floor = _aware(opportunity.sales_start) if opportunity.sales_start else first
        start, end = period_containing(freq, max(first, floor))

    out = []
    while end <= now and len(out) < MAX_BACKFILL_PERIODS:
        out.append((start, end))
        start, end = next_period(freq, end)
    return out


def run_due_distributions(now=None, actor=None) -> dict:
    from apps.investments.models import InvestmentOpportunity
    report = {'created': 0, 'posted': 0, 'drafts': 0, 'skipped': 0, 'errors': 0}
    opps = InvestmentOpportunity.objects.exclude(status=InvestmentOpportunity.Status.DRAFT)
    for opp in opps:
        for start, end in due_periods(opp, now):
            try:
                dist, created = create_distribution(opp, start, end, actor=actor)
            except Exception:
                logger.exception('investments: distribution failed for %s %s', opp.pk, start)
                report['errors'] += 1
                continue
            if not created:
                continue
            report['created'] += 1
            report[{'posted': 'posted', 'draft': 'drafts', 'skipped': 'skipped'}[dist.status]] += 1
    if report['drafts']:
        try:
            from apps.investments.services.notify import drafts_waiting
            drafts_waiting(report['drafts'])
        except Exception:
            logger.exception('investments: drafts notification failed')
    return report
