"""
apps/inventory/license_services.py
──────────────────────────────────
Business logic for licences. Views and the expiry command stay thin;
anything that changes state or touches more than one model lives here.

Public entry points
═══════════════════

  ┌─ Records ────────────────────────────────────────────────────────┐
  │ create_license(...)          — with the opening audit event      │
  │ log_license_update(...)      — after an edit form saves          │
  └──────────────────────────────────────────────────────────────────┘

  ┌─ Seats (this is where cost-per-user comes from) ─────────────────┐
  │ assign_seat(license, user=…) / release_seat(seat)                │
  │ sync_seats_from_users(license, users)                            │
  └──────────────────────────────────────────────────────────────────┘

  ┌─ Lifecycle ──────────────────────────────────────────────────────┐
  │ renew_license(...)   suspend_license(...)  reactivate_license(...)│
  │ cancel_license(...)  refresh_status(...)                          │
  └──────────────────────────────────────────────────────────────────┘

  ┌─ Alerts ─────────────────────────────────────────────────────────┐
  │ run_expiry_scan(...) — the daily job. Idempotent: each threshold  │
  │ fires exactly once per term, tracked on the licence itself.       │
  └──────────────────────────────────────────────────────────────────┘

  ┌─ Reporting ──────────────────────────────────────────────────────┐
  │ license_cost_summary(qs)   upcoming_renewals(days)                │
  │ cost_by_vendor(qs)         seat_utilisation(qs)                   │
  └──────────────────────────────────────────────────────────────────┘

Every state change writes an event row, and notifications are queued
with `transaction.on_commit` so nothing is announced that later rolls
back.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from decimal import Decimal
from typing import Iterable, Optional

from django.db import transaction
from django.utils import timezone

from . import license_notifications as notify
from .license_models import (
    License, LicenseEvent, LicenseRenewal, LicenseSeat, _money,
)

logger = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════════════════════
# Helpers
# ════════════════════════════════════════════════════════════════════════════

def log_event(lic: License, kind: str, message: str, *, actor=None, **payload) -> LicenseEvent:
    """Append to the audit trail. Always safe to call."""
    return LicenseEvent.objects.create(
        license=lic, kind=kind, actor=actor,
        message=message[:400], payload=payload or {},
    )


def add_months(d: date, months: int) -> date:
    """Calendar-correct month arithmetic — 31 Jan + 1 month = 28/29 Feb."""
    if not months:
        return d
    month_index = d.month - 1 + months
    year = d.year + month_index // 12
    month = month_index % 12 + 1
    # Clamp the day to the last valid day of the target month.
    if month == 12:
        next_month_first = date(year + 1, 1, 1)
    else:
        next_month_first = date(year, month + 1, 1)
    last_day = (next_month_first - timedelta(days=1)).day
    return date(year, month, min(d.day, last_day))


# ════════════════════════════════════════════════════════════════════════════
# Records
# ════════════════════════════════════════════════════════════════════════════

@transaction.atomic
def create_license(lic: License, *, actor=None) -> License:
    """Save a new licence and open its audit trail."""
    lic.created_by = lic.created_by or actor
    lic.save()
    log_event(
        lic, LicenseEvent.Kind.CREATED,
        f'Licence created — {lic.seats} seat(s), '
        f'{"perpetual" if lic.is_perpetual else f"ends {lic.end_date}"}.',
        actor=actor,
        total_cost=str(lic.total_cost), currency=lic.currency,
    )
    transaction.on_commit(lambda: notify.notify_license_created(lic, actor=actor))
    return lic


@transaction.atomic
def log_license_update(lic: License, *, actor=None, changed: Iterable[str] = ()) -> License:
    """Called by the edit view after the form saves."""
    fields = ', '.join(sorted(changed)) if changed else 'details'
    log_event(lic, LicenseEvent.Kind.UPDATED, f'Updated {fields}.', actor=actor,
              changed=list(changed))
    return lic


# ════════════════════════════════════════════════════════════════════════════
# Seats
# ════════════════════════════════════════════════════════════════════════════

@transaction.atomic
def assign_seat(
    lic: License, *,
    user=None,
    person_name: str = '',
    person_email: str = '',
    device_label: str = '',
    asset=None,
    cost_override: Optional[Decimal] = None,
    note: str = '',
    actor=None,
    allow_overallocation: bool = False,
) -> LicenseSeat:
    """
    Give someone a seat.

    Refuses to over-allocate by default — that's the whole point of
    tracking seats. A manager can force it (`allow_overallocation`) when
    the vendor has been paid for extra seats that aren't recorded yet;
    the licence then shows as over-allocated until the count is fixed.
    """
    lic = License.objects.select_for_update().get(pk=lic.pk)

    if lic.status in (License.Status.CANCELLED, License.Status.SUPERSEDED):
        raise ValueError('This licence is closed — assign a seat on the replacement.')
    if not (user or person_name or device_label):
        raise ValueError('Say who or what the seat is for.')

    if user is not None:
        existing = lic.assignments.filter(user=user, released_at__isnull=True).first()
        if existing:
            return existing

    if lic.seats_free <= 0 and not allow_overallocation:
        raise ValueError(
            f'No seats left — all {lic.seats} are assigned. '
            f'Increase the seat count or release a seat first.'
        )

    seat = LicenseSeat.objects.create(
        license=lic, user=user,
        person_name=person_name[:160], person_email=person_email,
        device_label=device_label[:120], asset=asset,
        cost_override=cost_override, assigned_by=actor, note=note[:200],
    )
    log_event(
        lic, LicenseEvent.Kind.SEAT_ASSIGNED,
        f'Seat assigned to {seat.holder_label}.',
        actor=actor, seat_id=str(seat.pk),
        cost_per_seat=str(seat.effective_cost),
    )

    transaction.on_commit(lambda: notify.notify_seat_assigned(seat, actor=actor))
    if lic.seats_free <= 0:
        transaction.on_commit(lambda: notify.notify_seats_exhausted(lic))
    return seat


@transaction.atomic
def release_seat(seat: LicenseSeat, *, actor=None, reason: str = '') -> LicenseSeat:
    """Hand a seat back. The row stays — history is the point."""
    if seat.released_at:
        return seat
    seat.released_at = timezone.now()
    seat.released_by = actor
    seat.release_reason = reason[:200]
    seat.save(update_fields=['released_at', 'released_by', 'release_reason'])

    log_event(
        seat.license, LicenseEvent.Kind.SEAT_RELEASED,
        f'Seat released from {seat.holder_label}. {reason}'.strip(),
        actor=actor, seat_id=str(seat.pk), days_held=seat.days_held,
    )
    transaction.on_commit(lambda: notify.notify_seat_released(seat, actor=actor))
    return seat


@transaction.atomic
def sync_seats_from_users(lic: License, users: Iterable, *, actor=None) -> dict:
    """
    Make the seat list match exactly this set of users — assign the
    missing ones, release the rest. Handy after an onboarding batch or a
    department move.
    """
    wanted = {u.pk: u for u in users if u}
    current = {s.user_id: s for s in lic.assignments.filter(released_at__isnull=True)
               if s.user_id}

    added, removed = 0, 0
    for uid, seat in current.items():
        if uid not in wanted:
            release_seat(seat, actor=actor, reason='Removed by seat sync')
            removed += 1
    for uid, user in wanted.items():
        if uid not in current:
            assign_seat(lic, user=user, actor=actor, note='Added by seat sync')
            added += 1
    return {'added': added, 'released': removed}


# ════════════════════════════════════════════════════════════════════════════
# Lifecycle
# ════════════════════════════════════════════════════════════════════════════

@transaction.atomic
def renew_license(
    lic: License, *,
    actor=None,
    term_months: Optional[int] = None,
    new_end: Optional[date] = None,
    seats: Optional[int] = None,
    unit_cost: Optional[Decimal] = None,
    unit_price: Optional[Decimal] = None,
    invoice_ref: str = '',
    notes: str = '',
) -> LicenseRenewal:
    """
    Extend the term. Records what we paid this time, resets the reminder
    ladder so the next cycle warns again, and reactivates an expired or
    suspended licence.

    Renewal runs from the later of today and the current end date, so
    renewing early doesn't throw away paid-for days.
    """
    lic = License.objects.select_for_update().get(pk=lic.pk)
    previous_end = lic.end_date
    term = int(term_months or lic.renewal_term_months or 12)

    if new_end is None:
        anchor = max(lic.end_date, timezone.localdate()) if lic.end_date else timezone.localdate()
        new_end = add_months(anchor, term)
    if lic.end_date and new_end <= lic.end_date:
        raise ValueError('The new end date must be after the current one.')

    if seats is not None:
        lic.seats = int(seats)
    if unit_cost is not None:
        lic.unit_cost = _money(unit_cost)
    if unit_price is not None:
        lic.unit_price = _money(unit_price)

    lic.start_date = previous_end or lic.start_date
    lic.end_date = new_end
    lic.is_perpetual = False
    lic.status = License.Status.ACTIVE
    lic.is_active = True
    # New term → the ladder starts again.
    lic.reminders_sent = []
    lic.last_reminder_at = None
    lic.expired_notified_at = None
    lic.save(update_fields=[
        'seats', 'unit_cost', 'unit_price', 'start_date', 'end_date',
        'is_perpetual', 'status', 'is_active', 'reminders_sent',
        'last_reminder_at', 'expired_notified_at', 'updated_at',
    ])

    renewal = LicenseRenewal.objects.create(
        license=lic,
        previous_end=previous_end,
        new_end=new_end,
        term_months=term,
        seats=lic.seats,
        unit_cost=lic.unit_cost,
        unit_price=lic.unit_price,
        total_cost=lic.total_cost,
        total_price=lic.total_price,
        currency=lic.currency,
        invoice_ref=invoice_ref[:100],
        notes=notes[:300],
        renewed_by=actor,
    )
    log_event(
        lic, LicenseEvent.Kind.RENEWED,
        f'Renewed to {new_end} ({term} month(s), {lic.seats} seat(s), '
        f'{lic.currency} {lic.total_cost}).',
        actor=actor, renewal_id=str(renewal.pk),
    )
    transaction.on_commit(lambda: notify.notify_license_renewed(lic, renewal, actor=actor))
    return renewal


@transaction.atomic
def suspend_license(lic: License, *, actor=None, reason: str = '') -> License:
    lic.status = License.Status.SUSPENDED
    lic.save(update_fields=['status', 'updated_at'])
    log_event(lic, LicenseEvent.Kind.SUSPENDED, reason or 'Licence suspended.', actor=actor)
    return lic


@transaction.atomic
def reactivate_license(lic: License, *, actor=None, note: str = '') -> License:
    lic.status = License.Status.ACTIVE
    lic.is_active = True
    lic.save(update_fields=['status', 'is_active', 'updated_at'])
    log_event(lic, LicenseEvent.Kind.REACTIVATED, note or 'Licence reactivated.', actor=actor)
    return lic


@transaction.atomic
def cancel_license(lic: License, *, actor=None, reason: str = '',
                   release_seats: bool = True) -> License:
    """Terminal. Optionally frees every seat so reports stop counting them."""
    lic.status = License.Status.CANCELLED
    lic.is_active = False
    lic.alerts_enabled = False
    lic.save(update_fields=['status', 'is_active', 'alerts_enabled', 'updated_at'])

    if release_seats:
        for seat in lic.assignments.filter(released_at__isnull=True):
            release_seat(seat, actor=actor, reason='Licence cancelled')

    log_event(lic, LicenseEvent.Kind.CANCELLED, reason or 'Licence cancelled.', actor=actor)
    return lic


def refresh_status(lic: License, *, save: bool = True) -> str:
    """
    Move ACTIVE → EXPIRED once the end date (plus grace) has passed. Kept
    separate from the notification path so a read-only page can call it.
    """
    if lic.is_perpetual or not lic.end_date:
        return lic.status
    if lic.status not in License.LIVE_STATUSES:
        return lic.status
    if lic.expiry_date and timezone.localdate() > lic.expiry_date:
        lic.status = License.Status.EXPIRED
        if save:
            lic.save(update_fields=['status', 'updated_at'])
    return lic.status


# ════════════════════════════════════════════════════════════════════════════
# The daily expiry scan
# ════════════════════════════════════════════════════════════════════════════

def run_expiry_scan(*, dry_run: bool = False, force: bool = False,
                    actor=None) -> dict:
    """
    Walk every live licence, fire the reminders that have come due, and
    flip anything past its grace period to EXPIRED.

    Idempotent — each threshold is recorded on the licence as it fires, so
    running the job twice in a day sends nothing twice. `force` re-sends
    the current threshold (useful when testing mail), `dry_run` reports
    what would go out without sending or writing anything.

    Returns a summary dict, which the management command prints and the
    "run now" button shows as a message.
    """
    today = timezone.localdate()
    summary = {
        'checked': 0, 'reminders': 0, 'expired': 0,
        'skipped': 0, 'dry_run': dry_run, 'ran_at': timezone.now(),
        'details': [],
    }

    qs = (License.objects
          .filter(is_active=True, is_perpetual=False, end_date__isnull=False)
          .filter(status__in=License.LIVE_STATUSES)
          .select_related('owner', 'holder_user', 'vendor', 'license_type')
          .order_by('end_date'))

    for lic in qs:
        summary['checked'] += 1

        if not lic.alerts_enabled and not force:
            summary['skipped'] += 1
            continue

        days = lic.days_remaining

        # ── Still running: has a reminder threshold been crossed? ───────────
        if days is not None and days >= 0:
            crossed = lic.crossed_reminders
            if force and not crossed:
                ladder = lic.get_reminder_days()
                crossed = [min((t for t in ladder if days <= t), default=days)]
            if not crossed:
                continue

            threshold = crossed[0]
            summary['reminders'] += 1
            summary['details'].append({
                'reference': lic.reference, 'name': lic.name,
                'action': 'reminder', 'days_left': days, 'threshold': threshold,
            })
            if dry_run:
                continue

            try:
                with transaction.atomic():
                    fired = sorted(lic.fired_reminders | set(crossed), reverse=True)
                    lic.reminders_sent = fired
                    lic.last_reminder_at = timezone.now()
                    lic.save(update_fields=['reminders_sent', 'last_reminder_at',
                                            'updated_at'])
                    log_event(
                        lic, LicenseEvent.Kind.REMINDER,
                        f'Expiry reminder sent — {days} day(s) left '
                        f'(threshold {threshold}).',
                        actor=actor, days_left=days, threshold=threshold,
                    )
                notify.notify_license_expiring(lic, days_left=days)
            except Exception:
                logger.exception('Licence reminder failed for %s', lic.reference)
            continue

        # ── Past the end date ───────────────────────────────────────────────
        already_told = lic.expired_notified_at is not None
        if already_told and not force:
            summary['skipped'] += 1
            continue

        summary['expired'] += 1
        summary['details'].append({
            'reference': lic.reference, 'name': lic.name,
            'action': 'expired', 'days_left': days, 'threshold': None,
        })
        if dry_run:
            continue

        try:
            with transaction.atomic():
                lic.expired_notified_at = timezone.now()
                fields = ['expired_notified_at', 'updated_at']
                if lic.expiry_date and today > lic.expiry_date:
                    lic.status = License.Status.EXPIRED
                    fields.append('status')
                lic.save(update_fields=fields)
                log_event(
                    lic, LicenseEvent.Kind.EXPIRED,
                    f'Licence expired on {lic.end_date}.', actor=actor,
                )
            notify.notify_license_expired(lic)
        except Exception:
            logger.exception('Licence expiry handling failed for %s', lic.reference)

    return summary


# ════════════════════════════════════════════════════════════════════════════
# Reporting helpers
# ════════════════════════════════════════════════════════════════════════════

def license_cost_summary(queryset) -> dict:
    """
    Roll up the money for a set of licences. Everything is computed in
    Python because the per-seat rules live on the model — the volumes here
    are hundreds of rows, not millions.
    """
    total_cost = total_price = annual_cost = annual_price = wasted = Decimal('0.00')
    seats = assigned = 0
    currencies = set()

    for lic in queryset:
        total_cost += lic.total_cost
        total_price += lic.total_price
        annual_cost += lic.annualised_cost
        annual_price += lic.annualised_price
        wasted += lic.wasted_cost
        seats += lic.seats
        assigned += lic.seats_assigned
        currencies.add(lic.currency)

    return {
        'count':          len(queryset) if hasattr(queryset, '__len__') else queryset.count(),
        'total_cost':     _money(total_cost),
        'total_price':    _money(total_price),
        'margin':         _money(total_price - total_cost),
        'annual_cost':    _money(annual_cost),
        'annual_price':   _money(annual_price),
        'monthly_cost':   _money(annual_cost / 12) if annual_cost else Decimal('0.00'),
        'wasted_cost':    _money(wasted),
        'seats':          seats,
        'seats_assigned': assigned,
        'seats_free':     max(seats - assigned, 0),
        'utilisation':    round(assigned * 100 / seats, 1) if seats else 0.0,
        'currency':       currencies.pop() if len(currencies) == 1 else 'mixed',
    }


def upcoming_renewals(days: int = 90, *, queryset=None):
    """Live licences ending inside the window, soonest first."""
    today = timezone.localdate()
    qs = queryset if queryset is not None else License.objects.filter(is_active=True)
    return (qs
            .filter(is_perpetual=False, end_date__isnull=False,
                    end_date__gte=today, end_date__lte=today + timedelta(days=days),
                    status__in=License.LIVE_STATUSES)
            .select_related('vendor', 'owner', 'holder_user')
            .order_by('end_date'))


def expired_licenses(*, queryset=None):
    today = timezone.localdate()
    qs = queryset if queryset is not None else License.objects.filter(is_active=True)
    return (qs
            .filter(is_perpetual=False, end_date__isnull=False, end_date__lt=today)
            .exclude(status__in=[License.Status.CANCELLED, License.Status.SUPERSEDED])
            .select_related('vendor', 'owner')
            .order_by('end_date'))


def cost_by_vendor(queryset) -> list[dict]:
    """Annualised spend grouped by vendor — biggest first."""
    buckets: dict[str, dict] = {}
    for lic in queryset:
        key = lic.vendor.name if lic.vendor_id else 'No vendor recorded'
        row = buckets.setdefault(key, {
            'vendor': key, 'count': 0,
            'annual_cost': Decimal('0.00'), 'seats': 0,
        })
        row['count'] += 1
        row['annual_cost'] += lic.annualised_cost
        row['seats'] += lic.seats
    rows = sorted(buckets.values(), key=lambda r: r['annual_cost'], reverse=True)
    for r in rows:
        r['annual_cost'] = _money(r['annual_cost'])
    return rows


def seat_utilisation(queryset) -> list[dict]:
    """Per-licence seat usage, worst utilisation first — where to cut."""
    rows = []
    for lic in queryset:
        if not lic.is_per_seat or not lic.seats:
            continue
        rows.append({
            'license': lic,
            'assigned': lic.seats_assigned,
            'seats': lic.seats,
            'pct': lic.seat_fill_pct,
            'wasted': lic.wasted_cost,
        })
    return sorted(rows, key=lambda r: (r['pct'], -float(r['wasted'])))


def licenses_for_user(user):
    """Every live seat this person is holding — powers "My licences"."""
    return (LicenseSeat.objects
            .filter(user=user, released_at__isnull=True)
            .select_related('license', 'license__vendor')
            .order_by('license__end_date'))
