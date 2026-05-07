"""
apps/finance/signals.py
========================

Automatic audit-trail capture + lightweight anomaly detection.

How it works
------------
We hook post_save / post_delete on every financial model (Payment,
PaymentRequest, IncomingPaymentRequest, PurchaseRequest,
EmployeeFinanceRequest, EmployeeLoan, Budget, Contract). For each event we:

1. Create a FinanceAuditLog row with the actor (pulled from the
   FinanceAuditMiddleware thread-local), object snapshot, and field-level
   diff.
2. Run the anomaly detector against the new state. If anything fires,
   create a FinanceAnomaly row.

The actor comes from `apps.finance.middleware.get_current_actor()`. If it's
missing (e.g. background task, management command), we record "System" as
the actor.

Field diffing
-------------
We compare pre-save (cached on instance as `_audit_old`) vs post-save state.
For brand-new records we just record the create. For deletes we snapshot
the final state.
"""
import logging
from datetime import timedelta
from decimal import Decimal

from django.db.models.signals import pre_save, post_save, post_delete
from django.dispatch import receiver
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
    FinanceAuditLog,
    FinanceAnomaly,
)
from apps.finance.middleware import get_current_actor, get_current_request

logger = logging.getLogger(__name__)


# Models we audit and what fields to snapshot. Excluded fields are noise
# (timestamps, auto-fields, M2M handled separately). Field names match
# exactly what's defined on each model in apps/finance/models.py.
AUDITED_MODELS = {
    Payment: [
        'reference', 'amount', 'currency', 'method', 'direction',
        'payment_type', 'recipient', 'payment_date', 'budget_id',
        'project_id', 'employee_id', 'notes',
    ],
    PaymentRequest: [
        'title', 'amount', 'currency', 'status', 'recipient_type',
        'recipient_user_id', 'recipient_name', 'due_date',
        'budget_id', 'project_id', 'payment_type',
    ],
    IncomingPaymentRequest: [
        'invoice_number', 'title', 'customer_name', 'amount',
        'tax_amount', 'discount_amount', 'currency', 'issue_date',
        'due_date', 'status', 'paid_at',
    ],
    PurchaseRequest: [
        'title', 'estimated_cost', 'actual_cost', 'status',
        'priority', 'vendor', 'budget_id', 'project_id',
    ],
    EmployeeFinanceRequest: [
        'request_type', 'title', 'amount_requested', 'amount_approved',
        'status', 'employee_id', 'budget_id',
    ],
    EmployeeLoan: [
        'principal_amount', 'approved_amount', 'disbursed_amount',
        'amount_repaid', 'status', 'employee_id',
    ],
    Budget: [
        'name', 'fiscal_year', 'total_amount', 'allocated_amount',
        'spent_amount', 'status', 'department_id', 'unit_id',
    ],
    Contract: [
        'title', 'contract_type', 'standard_cost', 'currency',
        'billing_cycle', 'start_date', 'end_date', 'status',
        'staff_id', 'vendor_company',
    ],
}


def _snapshot(instance, fields):
    """Plain-dict snapshot of selected fields, JSON-safe."""
    snap = {}
    for f in fields:
        try:
            v = getattr(instance, f, None)
            if isinstance(v, Decimal):
                snap[f] = str(v)
            elif hasattr(v, 'isoformat'):
                snap[f] = v.isoformat()
            elif v is None or isinstance(v, (str, int, float, bool)):
                snap[f] = v
            else:
                snap[f] = str(v)
        except Exception:
            snap[f] = None
    return snap


def _diff(old: dict, new: dict) -> dict:
    """Return {field: [old, new]} for fields that changed."""
    out = {}
    for k, v in new.items():
        if old.get(k) != v:
            out[k] = [old.get(k), v]
    return out


def _amount_of(instance):
    for attr in (
        'amount', 'amount_requested', 'amount_approved',
        'estimated_cost', 'standard_cost', 'total_amount',
        'principal_amount',
    ):
        if hasattr(instance, attr):
            v = getattr(instance, attr)
            if v is not None:
                return v
    return None


def _currency_of(instance):
    return getattr(instance, 'currency', '') or ''


def _actor_data():
    """
    Pull (user, name, ip, ua) from the middleware thread-local.

    Uses get_current_actor / get_current_request from apps.finance.middleware,
    matching the existing accessor pattern in the project.
    """
    user = get_current_actor()
    if user is None:
        return None, '', '', ''

    name = (user.get_full_name() or user.username) if user else ''

    req = get_current_request()
    ip = ''
    ua = ''
    if req is not None:
        meta = getattr(req, 'META', {}) or {}
        xff = meta.get('HTTP_X_FORWARDED_FOR', '')
        ip = xff.split(',')[0].strip() if xff else meta.get('REMOTE_ADDR', '')
        ua = (meta.get('HTTP_USER_AGENT', '') or '')[:300]

    return user, name, ip, ua


# ─── Pre-save: cache the old state on the instance ───────────────────────────

@receiver(pre_save)
def cache_old_state(sender, instance, **kwargs):
    if sender not in AUDITED_MODELS or instance.pk is None:
        return
    try:
        old = sender.objects.get(pk=instance.pk)
        instance._audit_old = _snapshot(old, AUDITED_MODELS[sender])
    except sender.DoesNotExist:
        instance._audit_old = None


# ─── Post-save: write audit log + run anomaly checks ─────────────────────────

@receiver(post_save)
def log_save(sender, instance, created, **kwargs):
    if sender not in AUDITED_MODELS:
        return

    fields = AUDITED_MODELS[sender]
    new = _snapshot(instance, fields)
    user, name, ip, ua = _actor_data()

    if created:
        action = FinanceAuditLog.Action.CREATE
        changes = {}
    else:
        old = getattr(instance, '_audit_old', None) or {}
        changes = _diff(old, new)
        if not changes:
            return  # nothing meaningful changed
        # Pick a more specific action verb if a status field flipped
        action = FinanceAuditLog.Action.UPDATE
        if 'status' in changes:
            new_status = (changes['status'][1] or '').lower()
            mapping = {
                'paid':         FinanceAuditLog.Action.PAY,
                'sent':         FinanceAuditLog.Action.SEND,
                'cancelled':    FinanceAuditLog.Action.CANCEL,
                'rejected':     FinanceAuditLog.Action.REJECT,
                'ceo_approved': FinanceAuditLog.Action.APPROVE,
                'approved':     FinanceAuditLog.Action.APPROVE,
            }
            action = mapping.get(new_status, action)

    try:
        entry = FinanceAuditLog.objects.create(
            actor=user,
            actor_name=name,
            ip_address=ip or None,
            user_agent=ua,
            action=action,
            model_name=sender.__name__,
            object_id=str(instance.pk),
            object_repr=str(instance)[:300],
            amount=_amount_of(instance),
            currency=_currency_of(instance),
            changes=changes,
        )
    except Exception:
        logger.exception('Failed to write FinanceAuditLog')
        return

    # Run anomaly checks (best-effort; never raise to caller)
    try:
        _detect_anomalies(sender, instance, created, changes, user, entry)
    except Exception:
        logger.exception('Anomaly detection failed for %s', sender.__name__)


@receiver(post_delete)
def log_delete(sender, instance, **kwargs):
    if sender not in AUDITED_MODELS:
        return
    user, name, ip, ua = _actor_data()
    final = _snapshot(instance, AUDITED_MODELS[sender])
    try:
        FinanceAuditLog.objects.create(
            actor=user, actor_name=name, ip_address=ip or None, user_agent=ua,
            action=FinanceAuditLog.Action.DELETE,
            model_name=sender.__name__,
            object_id=str(instance.pk),
            object_repr=str(instance)[:300],
            amount=_amount_of(instance),
            currency=_currency_of(instance),
            changes={'_deleted': [final, None]},
            notes='Record deleted.',
        )
    except Exception:
        logger.exception('Failed to write delete audit')


# ─── Anomaly detection ───────────────────────────────────────────────────────

# Tunables — adjust per organization size
LARGE_PAYMENT_THRESHOLD = Decimal('500000')   # GMD
DUPLICATE_WINDOW_HOURS  = 24
RAPID_APPROVAL_SECONDS  = 120                  # approve within 2 min of submit


def _flag(kind, severity, title, description, instance, audit_entry, **extra):
    try:
        FinanceAnomaly.objects.create(
            kind=kind,
            severity=severity,
            title=title[:200],
            description=description,
            model_name=type(instance).__name__,
            object_id=str(instance.pk) if instance.pk else '',
            audit_entry=audit_entry,
            amount=_amount_of(instance),
            currency=_currency_of(instance),
            extra=extra,
        )
    except Exception:
        logger.exception('Failed to flag anomaly')


def _detect_anomalies(sender, instance, created, changes, actor, audit_entry):
    now = timezone.now()

    # 1. Off-hours edit (any model, any change) — outside Mon-Fri 7am-7pm
    if (now.hour < 7 or now.hour >= 19) or now.weekday() >= 5:
        amt = _amount_of(instance) or Decimal('0')
        if amt and amt >= Decimal('50000'):  # only flag for material amounts
            _flag(
                FinanceAnomaly.Kind.OFF_HOURS_EDIT,
                FinanceAnomaly.Severity.MEDIUM,
                f'{sender.__name__} edited outside business hours',
                f'A material {sender.__name__.lower()} of {amt} was '
                f'{"created" if created else "modified"} at {now.strftime("%a %H:%M")}.',
                instance, audit_entry,
                hour=now.hour, weekday=now.weekday(),
            )

    # 2. Weekend payment
    if sender is Payment and created:
        pdate = getattr(instance, 'payment_date', None)
        if pdate and pdate.weekday() >= 5:
            _flag(
                FinanceAnomaly.Kind.WEEKEND_PAYMENT,
                FinanceAnomaly.Severity.LOW,
                f'Payment dated on a weekend ({pdate.strftime("%A")})',
                f'Payment {instance.reference} of {instance.amount} {instance.currency} '
                f'has payment_date {pdate.isoformat()} which falls on a weekend.',
                instance, audit_entry,
            )

    # 3. Large amount
    amt = _amount_of(instance)
    if created and amt and amt >= LARGE_PAYMENT_THRESHOLD:
        _flag(
            FinanceAnomaly.Kind.LARGE_AMOUNT,
            FinanceAnomaly.Severity.HIGH,
            f'Large {sender.__name__} amount: {amt}',
            f'A new {sender.__name__.lower()} was created for {amt} '
            f'{_currency_of(instance)}, above the {LARGE_PAYMENT_THRESHOLD} threshold.',
            instance, audit_entry,
        )

    # 4. Duplicate payment amount within window
    if sender is Payment and created and instance.amount:
        dupe = Payment.objects.filter(
            amount=instance.amount,
            recipient=instance.recipient,
            payment_date__gte=now.date() - timedelta(days=1),
        ).exclude(pk=instance.pk).exists()
        if dupe:
            _flag(
                FinanceAnomaly.Kind.DUPLICATE_AMOUNT,
                FinanceAnomaly.Severity.HIGH,
                f'Possible duplicate payment to {instance.recipient}',
                f'A payment of {instance.amount} to "{instance.recipient}" was '
                f'recorded within the last {DUPLICATE_WINDOW_HOURS}h.',
                instance, audit_entry,
            )

    # 5. Rapid self-approval (PurchaseRequest, EmployeeFinanceRequest)
    if sender in (PurchaseRequest, EmployeeFinanceRequest) and 'status' in changes:
        new_status = changes['status'][1]
        if new_status in ('ceo_approved', 'approved'):
            requester_id = (
                getattr(instance, 'requested_by_id', None)
                or getattr(instance, 'employee_id', None)
            )
            if actor and actor.pk == requester_id:
                _flag(
                    FinanceAnomaly.Kind.RAPID_APPROVAL,
                    FinanceAnomaly.Severity.HIGH,
                    'Self-approval detected',
                    f'{actor} approved a {sender.__name__.lower()} they submitted themselves.',
                    instance, audit_entry,
                )
            elif getattr(instance, 'created_at', None):
                gap = (now - instance.created_at).total_seconds()
                if gap < RAPID_APPROVAL_SECONDS:
                    _flag(
                        FinanceAnomaly.Kind.RAPID_APPROVAL,
                        FinanceAnomaly.Severity.MEDIUM,
                        f'Approval within {int(gap)}s of submission',
                        f'{sender.__name__} approved {int(gap)} seconds after creation — '
                        'unusually fast review.',
                        instance, audit_entry, gap_seconds=int(gap),
                    )

    # 6. Backdated payment (payment_date more than 30 days in the past at create)
    if sender is Payment and created:
        pdate = getattr(instance, 'payment_date', None)
        if pdate and (now.date() - pdate).days > 30:
            _flag(
                FinanceAnomaly.Kind.BACKDATED,
                FinanceAnomaly.Severity.MEDIUM,
                f'Payment backdated by {(now.date() - pdate).days} days',
                f'Payment {instance.reference} created today with payment_date '
                f'{pdate.isoformat()}.',
                instance, audit_entry,
            )

    # 7. Budget overrun on the linked budget
    if sender is Payment and created and getattr(instance, 'budget_id', None):
        try:
            b = instance.budget
            if b and b.total_amount and b.spent_amount > b.total_amount:
                _flag(
                    FinanceAnomaly.Kind.BUDGET_OVERRUN,
                    FinanceAnomaly.Severity.HIGH,
                    f'Budget "{b.name}" exceeded',
                    f'Budget "{b.name}" is over its allocation: spent '
                    f'{b.spent_amount} of {b.total_amount}.',
                    instance, audit_entry,
                    budget_id=str(b.id),
                )
        except Exception:
            pass