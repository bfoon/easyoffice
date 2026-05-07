"""
Anomaly detection rules.

Two trigger styles:
  * Real-time — cheap rules that run inside the audit write path.
  * Batch    — expensive rules run by the nightly management command
               (apps/finance/management/commands/run_anomaly_rules.py).

Rules MUST never raise. A rule failure must not block the original
transaction. Every rule is wrapped in a try/except in run_realtime_rules.
"""
from __future__ import annotations

import logging
from datetime import time, timedelta
from decimal import Decimal

from django.db.models import Count, Sum
from django.utils import timezone

from .models import FinancialAnomaly, FinancialAuditEntry

log = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════════════════════
#  Real-time rules — fire on each audit entry write
# ════════════════════════════════════════════════════════════════════════════
def detect_round_number(audit_entry: FinancialAuditEntry) -> None:
    """Flag amounts >= 10,000 that are exact multiples of 1,000."""
    amt = audit_entry.amount
    if amt is None:
        return
    if amt >= Decimal('10000') and amt % Decimal('1000') == 0:
        FinancialAnomaly.objects.create(
            audit_entry=audit_entry,
            kind=FinancialAnomaly.Kind.ROUND_NUMBER,
            severity=FinancialAnomaly.Severity.LOW,
            title=f'Suspiciously round amount: {audit_entry.currency} {amt:,.0f}',
            description=(
                f'A {audit_entry.model_name.lower()} for exactly '
                f'{audit_entry.currency} {amt:,.0f} — large round numbers can '
                f'indicate placeholder or fabricated values worth a quick review.'
            ),
            amount=amt,
            currency=audit_entry.currency,
        )


def detect_after_hours(audit_entry: FinancialAuditEntry) -> None:
    """Flag financial edits made before 07:00 or after 20:00 local time."""
    local_t = audit_entry.timestamp.astimezone(
        timezone.get_current_timezone()
    ).time()
    if local_t < time(7, 0) or local_t >= time(20, 0):
        FinancialAnomaly.objects.create(
            audit_entry=audit_entry,
            kind=FinancialAnomaly.Kind.AFTER_HOURS_EDIT,
            severity=FinancialAnomaly.Severity.MEDIUM,
            title='After-hours financial edit',
            description=(
                f'{audit_entry.actor_name or "Unknown user"} edited a '
                f'{audit_entry.model_name.lower()} at '
                f'{local_t.strftime("%H:%M")}. Outside business hours edits '
                f'should be reviewed.'
            ),
            amount=audit_entry.amount,
            currency=audit_entry.currency,
        )


REALTIME_RULES = [
    detect_round_number,
    detect_after_hours,
]


def run_realtime_rules(audit_entry: FinancialAuditEntry) -> None:
    """Run every real-time rule. Failures are logged but never raised."""
    for rule in REALTIME_RULES:
        try:
            rule(audit_entry)
        except Exception:
            log.exception('Anomaly rule failed: %s', rule.__name__)


# ════════════════════════════════════════════════════════════════════════════
#  Batch rules — run nightly via management command
# ════════════════════════════════════════════════════════════════════════════
def detect_duplicate_invoices(window_days: int = 30) -> int:
    """Two or more finalized invoices to the same client for the same total
    within `window_days`. Returns count of anomalies created."""
    try:
        from apps.invoices.models import InvoiceDocument
    except ImportError:
        return 0

    cutoff = timezone.now().date() - timedelta(days=window_days)
    groups = (
        InvoiceDocument.objects
        .filter(status='finalized', invoice_date__gte=cutoff)
        .values('client_name', 'total')
        .annotate(n=Count('id'))
        .filter(n__gt=1)
    )

    created = 0
    for grp in groups:
        invoices = list(
            InvoiceDocument.objects.filter(
                client_name=grp['client_name'],
                total=grp['total'],
                status='finalized',
                invoice_date__gte=cutoff,
            )
        )
        # Skip if we already flagged this exact set of invoices recently
        existing = FinancialAnomaly.objects.filter(
            kind=FinancialAnomaly.Kind.DUPLICATE_INVOICE,
            status__in=['open', 'reviewing'],
            amount=grp['total'],
        ).filter(description__icontains=grp['client_name']).exists()
        if existing:
            continue

        FinancialAnomaly.objects.create(
            kind=FinancialAnomaly.Kind.DUPLICATE_INVOICE,
            severity=FinancialAnomaly.Severity.HIGH,
            title=f'Possible duplicate invoices to {grp["client_name"]}',
            description=(
                f'{grp["n"]} finalized invoices for the same amount '
                f'({grp["total"]}) issued to {grp["client_name"]} within '
                f'{window_days} days. Numbers: '
                f'{", ".join(i.number or "(draft)" for i in invoices)}.'
            ),
            amount=grp['total'],
            currency='GMD',
        )
        created += 1
    return created


def detect_vendor_concentration(threshold_pct: float = 30.0,
                                window_days: int = 90) -> int:
    """Flag any single counterparty that received > threshold_pct of total
    PAYMENT_OUT in the trailing window."""
    from .models import Transaction

    cutoff = timezone.now().date() - timedelta(days=window_days)
    total = Transaction.objects.filter(
        kind=Transaction.Kind.PAYMENT_OUT,
        cash_date__gte=cutoff,
    ).aggregate(s=Sum('amount'))['s'] or Decimal('0')

    if total <= 0:
        return 0

    by_party = (
        Transaction.objects
        .filter(kind=Transaction.Kind.PAYMENT_OUT, cash_date__gte=cutoff)
        .exclude(counterparty='')
        .values('counterparty')
        .annotate(total=Sum('amount'))
        .order_by('-total')
    )

    threshold = Decimal(str(threshold_pct)) / Decimal('100')
    created = 0
    for row in by_party:
        share = row['total'] / total
        if share <= threshold:
            continue
        # Avoid duplicates — one open anomaly per vendor at a time
        if FinancialAnomaly.objects.filter(
            kind=FinancialAnomaly.Kind.VENDOR_CONCENTRATION,
            status__in=['open', 'reviewing'],
            description__icontains=row['counterparty'],
        ).exists():
            continue

        FinancialAnomaly.objects.create(
            kind=FinancialAnomaly.Kind.VENDOR_CONCENTRATION,
            severity=FinancialAnomaly.Severity.MEDIUM,
            title=f'Vendor concentration: {row["counterparty"]}',
            description=(
                f'{row["counterparty"]} received {share * 100:.1f}% of total '
                f'outflows ({row["total"]:,.0f} of {total:,.0f}) over the past '
                f'{window_days} days — above the {threshold_pct:.0f}% threshold.'
            ),
            amount=row['total'],
            currency='GMD',
        )
        created += 1
    return created


def detect_void_and_reissue(window_hours: int = 24) -> int:
    """An invoice voided then re-issued to the same client for a similar
    amount within the window. Suggests amount manipulation."""
    try:
        from apps.invoices.models import InvoiceDocument
    except ImportError:
        return 0

    cutoff = timezone.now() - timedelta(hours=window_hours)
    voided = InvoiceDocument.objects.filter(
        status='voided', voided_at__gte=cutoff,
    ).select_related()

    created = 0
    for v in voided:
        if not v.client_name:
            continue
        candidate = InvoiceDocument.objects.filter(
            status='finalized',
            client_name=v.client_name,
            invoice_date__gte=v.voided_at.date() if v.voided_at else cutoff.date(),
        ).exclude(pk=v.pk).first()
        if not candidate:
            continue

        # Already flagged?
        if FinancialAnomaly.objects.filter(
            kind=FinancialAnomaly.Kind.VOID_AND_REISSUE,
            description__icontains=v.number or str(v.pk),
        ).exists():
            continue

        FinancialAnomaly.objects.create(
            kind=FinancialAnomaly.Kind.VOID_AND_REISSUE,
            severity=FinancialAnomaly.Severity.HIGH,
            title=f'Void → re-issue pattern for {v.client_name}',
            description=(
                f'Invoice {v.number} (voided) followed by {candidate.number} '
                f'(finalized) to the same client within {window_hours}h. '
                f'Amounts: {v.total} → {candidate.total}. Review for '
                f'unauthorised amount changes.'
            ),
            amount=candidate.total,
            currency=candidate.currency,
        )
        created += 1
    return created


BATCH_RULES = [
    detect_duplicate_invoices,
    detect_vendor_concentration,
    detect_void_and_reissue,
]


def run_batch_rules() -> dict:
    """Run every batch rule. Returns a {rule_name: count_created} dict."""
    out = {}
    for rule in BATCH_RULES:
        try:
            out[rule.__name__] = rule()
        except Exception:
            log.exception('Batch rule failed: %s', rule.__name__)
            out[rule.__name__] = -1
    return out
