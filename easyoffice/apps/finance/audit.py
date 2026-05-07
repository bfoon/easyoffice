"""
Audit helpers used by every signal handler.

write_audit() builds a FinancialAuditEntry, captures actor + IP from the
thread-local request, and triggers the real-time anomaly rule pass.
"""
from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any, Iterable

from django.contrib.contenttypes.models import ContentType

from .anomalies import run_realtime_rules
from .models import FinancialAuditEntry

log = logging.getLogger(__name__)


# Map class name -> human label shown in the audit log.
MODEL_DISPLAY = {
    'InvoiceDocument':   'Invoice',
    'Payment':           'Payment',
    'PaymentAllocation': 'Payment Allocation',
    'Budget':            'Budget',
    'Contract':          'Contract',
    'Loan':              'Loan',
    'Transaction':       'Ledger Entry',
    'FinancialAnomaly':  'Anomaly',
    'FinanceCategory':   'Category',
    'Department':        'Department',
}


def write_audit(*,
                action: str,
                instance: Any,
                actor=None,
                amount: Decimal | None = None,
                currency: str = '',
                changes: dict | None = None,
                request=None,
                notes: str = '',
                run_anomalies: bool = True) -> FinancialAuditEntry:
    """Create one audit entry. All arguments are keyword-only on purpose —
    keeps call sites self-documenting."""
    cls = instance.__class__
    try:
        ct = ContentType.objects.get_for_model(cls)
    except Exception:
        ct = None

    model_name = MODEL_DISPLAY.get(cls.__name__, cls.__name__)

    ip = ua = None
    if request is not None:
        meta = getattr(request, 'META', {}) or {}
        ip = meta.get('REMOTE_ADDR')
        ua = (meta.get('HTTP_USER_AGENT', '') or '')[:400]

    entry = FinancialAuditEntry.objects.create(
        action=action,
        actor=actor,
        actor_name=(getattr(actor, 'get_full_name', lambda: '')() or
                    getattr(actor, 'username', '') or ''),
        ip_address=ip,
        user_agent=ua or '',
        model_name=model_name,
        object_repr=str(instance)[:300],
        content_type=ct,
        object_id=str(getattr(instance, 'pk', '') or ''),
        amount=amount,
        currency=currency or '',
        changes=changes or {},
        notes=notes or '',
    )
    if run_anomalies:
        run_realtime_rules(entry)
    return entry


def capture_diff(old, new, fields: Iterable[str]) -> dict:
    """Returns {field: [old_repr, new_repr]} for changed fields only.

    All values are stringified so the result is JSON-safe and matches the
    [old, new] tuple shape the audit_entry detail template iterates.
    """
    diff: dict = {}
    for f in fields:
        ov = getattr(old, f, None)
        nv = getattr(new, f, None)
        if ov != nv:
            diff[f] = [_serialize(ov), _serialize(nv)]
    return diff


def _serialize(v):
    if v is None:
        return None
    if isinstance(v, Decimal):
        return f'{v:.2f}'
    return str(v)
