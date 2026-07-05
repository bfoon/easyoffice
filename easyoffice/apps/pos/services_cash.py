"""
apps/pos/services_cash.py
─────────────────────────
Service layer for cash-drawer sessions: open a shift, record pay-ins /
pay-outs, and close with a physical count that produces the over/short
variance.

The golden rule: a cashier may have at most ONE open session at a time.
Sales completed while a session is open are auto-linked to it (see the
one-line hook added to complete_sale), so cash_sales tallies itself.
"""
from __future__ import annotations

import logging
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from .services import D, POSError
from .models import POSSale
from .models_cash import (
    POSCashSession, POSCashMovement, POSCashSessionCounter,
)

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Lookup
# ─────────────────────────────────────────────────────────────────────────────

def active_session(user) -> POSCashSession | None:
    """The cashier's currently-open shift, or None."""
    return (POSCashSession.objects
            .filter(cashier=user, status=POSCashSession.Status.OPEN)
            .order_by('-opened_at')
            .first())


def require_active_session(user) -> POSCashSession:
    s = active_session(user)
    if s is None:
        raise POSError('No cash session is open. Open the drawer before selling.')
    return s


# ─────────────────────────────────────────────────────────────────────────────
# Open
# ─────────────────────────────────────────────────────────────────────────────

@transaction.atomic
def open_session(user, *, opening_float, register_name: str = '',
                 note: str = '', currency: str = 'GMD') -> POSCashSession:
    """
    Start a shift with a counted opening float. Refuses if the cashier
    already has an open session (close it first).
    """
    existing = active_session(user)
    if existing is not None:
        raise POSError(
            f'You already have an open session ({existing.session_no}). '
            f'Close it before opening a new one.'
        )

    opening = D(opening_float)
    if opening < 0:
        raise POSError('Opening float cannot be negative.')

    session = POSCashSession.objects.create(
        cashier=user,
        session_no=POSCashSessionCounter.next_session_no(),
        opening_float=opening,
        register_name=(register_name or '')[:60],
        open_note=(note or '')[:300],
        currency=currency or 'GMD',
        opened_at=timezone.now(),
    )
    return session


# ─────────────────────────────────────────────────────────────────────────────
# Mid-shift cash movements
# ─────────────────────────────────────────────────────────────────────────────

@transaction.atomic
def add_cash_movement(session: POSCashSession, *, kind: str, amount,
                      reason: str = '', actor=None) -> POSCashMovement:
    """Record a pay-in or pay-out against an open session."""
    session = POSCashSession.objects.select_for_update().get(pk=session.pk)
    if not session.is_open:
        raise POSError('This session is closed.')

    if kind not in POSCashMovement.Kind.values:
        raise POSError('Unknown cash movement type.')

    amt = D(amount)
    if amt <= 0:
        raise POSError('Amount must be greater than zero.')

    return POSCashMovement.objects.create(
        session=session,
        kind=kind,
        amount=amt,
        reason=(reason or '')[:300],
        actor=actor if (actor and getattr(actor, 'is_authenticated', False)) else None,
    )


def pay_in(session, amount, *, reason='', actor=None):
    return add_cash_movement(session, kind=POSCashMovement.Kind.PAY_IN,
                             amount=amount, reason=reason, actor=actor)


def pay_out(session, amount, *, reason='', actor=None):
    return add_cash_movement(session, kind=POSCashMovement.Kind.PAY_OUT,
                             amount=amount, reason=reason, actor=actor)


# ─────────────────────────────────────────────────────────────────────────────
# Close
# ─────────────────────────────────────────────────────────────────────────────

@transaction.atomic
def close_session(session: POSCashSession, *, counted_cash, actor=None,
                  note: str = '') -> POSCashSession:
    """
    Close the shift with the physically-counted cash. Snapshots the
    expected figure and the variance so history stays stable even if a
    sale is later voided.

    Refuses to close while the cashier still has a draft (open basket)
    that has items in it — finish or clear it first, so no rung-up cash
    is left unaccounted.
    """
    session = POSCashSession.objects.select_for_update().get(pk=session.pk)
    if not session.is_open:
        raise POSError('This session is already closed.')

    # Guard: don't close over a non-empty open basket.
    open_basket = (POSSale.objects
                   .filter(cashier=session.cashier,
                           status=POSSale.Status.DRAFT,
                           session=session)
                   .first())
    if open_basket and open_basket.items.exists():
        raise POSError(
            'You have an unfinished basket. Complete or clear it before '
            'closing the drawer.'
        )

    counted = D(counted_cash)
    if counted < 0:
        raise POSError('Counted cash cannot be negative.')

    expected = session.expected_cash_live
    session.counted_cash = counted
    session.expected_cash = expected
    session.variance = (counted - expected).quantize(Decimal('0.01'))
    session.status = POSCashSession.Status.CLOSED
    session.closed_at = timezone.now()
    session.closed_by = actor if (actor and getattr(actor, 'is_authenticated', False)) else session.cashier
    session.close_note = (note or '')[:300]
    session.save(update_fields=[
        'counted_cash', 'expected_cash', 'variance', 'status',
        'closed_at', 'closed_by', 'close_note', 'updated_at',
    ])
    return session


# ─────────────────────────────────────────────────────────────────────────────
# Reporting payload
# ─────────────────────────────────────────────────────────────────────────────

def session_payload(session: POSCashSession) -> dict:
    """Full reconciliation snapshot for the UI / Z-report / API."""
    expected = (session.expected_cash
                if session.expected_cash is not None
                else session.expected_cash_live)
    variance = (session.variance
                if session.variance is not None
                else session.variance_live)

    return {
        'id': str(session.pk),
        'session_no': session.session_no,
        'status': session.status,
        'cashier': (session.cashier.get_full_name()
                    or session.cashier.username) if session.cashier_id else '',
        'register_name': session.register_name,
        'currency': session.currency,

        'opening_float': str(session.opening_float),
        'opened_at': session.opened_at.isoformat() if session.opened_at else None,
        'open_note': session.open_note,

        # live tallies
        'cash_sales': str(session.cash_sales),
        'cash_pay_ins': str(session.cash_pay_ins),
        'cash_pay_outs': str(session.cash_pay_outs),
        'cash_refunds': str(session.cash_refunds),
        'noncash_by_method': {k: str(v) for k, v in
                              session.noncash_sales_by_method.items()},
        'total_sales': str(session.total_sales),
        'sales_count': session.sales_count,

        'expected_cash': str(expected),
        'counted_cash': (str(session.counted_cash)
                         if session.counted_cash is not None else None),
        'variance': (str(variance) if variance is not None else None),
        'variance_state': _variance_state(variance),

        'closed_at': session.closed_at.isoformat() if session.closed_at else None,
        'closed_by': (session.closed_by.get_full_name()
                      or session.closed_by.username) if session.closed_by_id else '',
        'close_note': session.close_note,

        'movements': [
            {
                'id': str(m.pk),
                'kind': m.kind,
                'amount': str(m.amount),
                'reason': m.reason,
                'at': m.created_at.isoformat(),
            }
            for m in session.movements.all()
        ],
    }


def _variance_state(variance):
    if variance is None:
        return 'pending'
    if variance == 0:
        return 'balanced'
    return 'over' if variance > 0 else 'short'
