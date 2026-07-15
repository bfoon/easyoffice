"""Business logic for equipment maintenance.

All state changes pass through this module so the timeline, notifications,
reminders, equipment state, invoice and receipt remain synchronized.
"""
from __future__ import annotations

import logging
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from .models import (
    Equipment,
    MaintenanceAccessToken,
    MaintenanceChargeLine,
    MaintenanceEvent,
    MaintenanceJob,
    MaintenanceReminder,
)
from . import maintenance_notifications as notifications
from . import customer_link

logger = logging.getLogger(__name__)


ALLOWED_TRANSITIONS = {
    MaintenanceJob.Status.INTAKE: {
        MaintenanceJob.Status.ASSIGNED,
        MaintenanceJob.Status.DIAGNOSIS,
        MaintenanceJob.Status.CANCELLED,
    },
    MaintenanceJob.Status.ASSIGNED: {
        MaintenanceJob.Status.DIAGNOSIS,
        MaintenanceJob.Status.CANCELLED,
    },
    MaintenanceJob.Status.DIAGNOSIS: {
        MaintenanceJob.Status.AWAITING_APPROVAL,
        MaintenanceJob.Status.WAITING_PARTS,
        MaintenanceJob.Status.IN_REPAIR,
        MaintenanceJob.Status.UNREPAIRABLE,
        MaintenanceJob.Status.CANCELLED,
    },
    MaintenanceJob.Status.AWAITING_APPROVAL: {
        MaintenanceJob.Status.WAITING_PARTS,
        MaintenanceJob.Status.IN_REPAIR,
        MaintenanceJob.Status.CANCELLED,
    },
    MaintenanceJob.Status.WAITING_PARTS: {
        MaintenanceJob.Status.IN_REPAIR,
        MaintenanceJob.Status.CANCELLED,
        MaintenanceJob.Status.UNREPAIRABLE,
    },
    MaintenanceJob.Status.IN_REPAIR: {
        MaintenanceJob.Status.WAITING_PARTS,
        MaintenanceJob.Status.QUALITY_CHECK,
        MaintenanceJob.Status.UNREPAIRABLE,
        MaintenanceJob.Status.CANCELLED,
    },
    MaintenanceJob.Status.QUALITY_CHECK: {
        MaintenanceJob.Status.IN_REPAIR,
        MaintenanceJob.Status.READY_PICKUP,
        MaintenanceJob.Status.UNREPAIRABLE,
    },
    MaintenanceJob.Status.READY_PICKUP: {
        MaintenanceJob.Status.COLLECTED,
        MaintenanceJob.Status.CLOSED,
    },
    MaintenanceJob.Status.COLLECTED: {
        MaintenanceJob.Status.CLOSED,
    },
    MaintenanceJob.Status.UNREPAIRABLE: {
        MaintenanceJob.Status.READY_PICKUP,
        MaintenanceJob.Status.CLOSED,
    },
    MaintenanceJob.Status.CANCELLED: {
        MaintenanceJob.Status.CLOSED,
    },
    MaintenanceJob.Status.CLOSED: set(),
}


def _decimal(value, default='0.00') -> Decimal:
    try:
        return Decimal(str(value if value not in (None, '') else default))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal(default)


def _default_deadlines(priority: str):
    now = timezone.now()
    hours = {
        MaintenanceJob.Priority.URGENT: (2, 8),
        MaintenanceJob.Priority.HIGH: (8, 24),
        MaintenanceJob.Priority.NORMAL: (24, 72),
        MaintenanceJob.Priority.LOW: (48, 120),
    }.get(priority, (24, 72))
    return now + timedelta(hours=hours[0]), now + timedelta(hours=hours[1])


def public_tracking_url(job: MaintenanceJob, request=None) -> str:
    token, _ = MaintenanceAccessToken.objects.get_or_create(job=job)
    path = reverse('maintenance_public', kwargs={'token': token.token})
    if request:
        return request.build_absolute_uri(path)
    base = getattr(settings, 'SITE_URL', '').rstrip('/')
    return f'{base}{path}' if base else path


def _event(job, kind, actor, message, *, public=True, old_status='', new_status='', payload=None):
    return MaintenanceEvent.objects.create(
        job=job,
        kind=kind,
        actor=actor if getattr(actor, 'is_authenticated', False) else None,
        message=message,
        is_public=public,
        old_status=old_status,
        new_status=new_status,
        payload=payload or {},
    )


def _owner_snapshot(payload, equipment=None, owner_user=None):
    owner_name = (payload.get('owner_name') or '').strip()
    owner_email = (payload.get('owner_email') or '').strip()
    owner_phone = (payload.get('owner_phone') or '').strip()

    if owner_user:
        owner_name = owner_name or getattr(owner_user, 'full_name', str(owner_user))
        owner_email = owner_email or getattr(owner_user, 'email', '')
        owner_phone = owner_phone or getattr(owner_user, 'phone', '') or ''
    elif equipment and equipment.current_user:
        owner_user = equipment.current_user
        owner_name = owner_name or getattr(owner_user, 'full_name', str(owner_user))
        owner_email = owner_email or getattr(owner_user, 'email', '')
        owner_phone = owner_phone or getattr(owner_user, 'phone', '') or ''

    return owner_user, owner_name, owner_email, owner_phone


@transaction.atomic
def create_maintenance_job(payload: dict, *, actor, request=None) -> MaintenanceJob:
    equipment = None
    equipment_id = payload.get('equipment_id')
    if equipment_id:
        equipment = Equipment.objects.select_for_update().filter(pk=equipment_id).first()

    owner_user = None
    owner_user_id = payload.get('owner_user_id')
    if owner_user_id:
        from apps.core.models import User
        owner_user = User.objects.filter(pk=owner_user_id).first()

    owner_user, owner_name, owner_email, owner_phone = _owner_snapshot(
        payload, equipment=equipment, owner_user=owner_user
    )
    if not owner_name:
        raise ValueError('Equipment owner/customer name is required.')

    # Link (or create) the customer-service contact for external owners, so a
    # walk-in lands in the contact book instead of being retyped every visit.
    # Staff-owned assets deliberately get no Customer row. Fail-soft: a CRM
    # problem must never block equipment intake.
    customer, customer_created = customer_link.resolve_customer_for_job(
        {**payload,
         'owner_name': owner_name,
         'owner_email': owner_email,
         'owner_phone': owner_phone},
        owner_user=owner_user,
        actor=actor,
    )

    priority = payload.get('priority') or MaintenanceJob.Priority.NORMAL
    diagnosis_due, promised_due = _default_deadlines(priority)

    job = MaintenanceJob.objects.create(
        ticket_id=payload.get('ticket_id') or None,
        equipment=equipment,
        equipment_name=(payload.get('equipment_name') or (equipment.name if equipment else '')).strip(),
        equipment_category=(payload.get('equipment_category') or (equipment.category if equipment else '')).strip(),
        make=(payload.get('make') or (equipment.make if equipment else '')).strip(),
        model=(payload.get('model') or (equipment.model if equipment else '')).strip(),
        serial_number=(payload.get('serial_number') or (equipment.serial_number if equipment else '')).strip(),
        asset_tag=(payload.get('asset_tag') or (equipment.asset_tag if equipment else '')).strip(),
        owner_user=owner_user,
        customer=customer,
        owner_name=owner_name,
        owner_email=owner_email,
        owner_phone=owner_phone,
        owner_company=(payload.get('owner_company') or '').strip(),
        owner_address=(payload.get('owner_address') or '').strip(),
        intake_channel=payload.get('intake_channel') or MaintenanceJob.IntakeChannel.WALK_IN,
        priority=priority,
        created_by=actor,
        assigned_to_id=payload.get('assigned_to_id') or None,
        reported_problem=(payload.get('reported_problem') or '').strip(),
        condition_received=(payload.get('condition_received') or '').strip(),
        accessories_received=(payload.get('accessories_received') or '').strip(),
        data_privacy_consent=bool(payload.get('data_privacy_consent')),
        backup_authorized=bool(payload.get('backup_authorized')),
        diagnosis_due_at=payload.get('diagnosis_due_at') or diagnosis_due,
        promised_completion_at=payload.get('promised_completion_at') or promised_due,
        currency=(payload.get('currency') or 'GMD')[:3].upper(),
        tax_rate=_decimal(payload.get('tax_rate')),
        discount_amount=_decimal(payload.get('discount_amount')),
        internal_note=(payload.get('internal_note') or '').strip(),
    )
    if not job.equipment_name:
        raise ValueError('Equipment name is required.')
    if not job.reported_problem:
        raise ValueError('Reported problem is required.')

    token = MaintenanceAccessToken.objects.create(job=job)

    if equipment:
        equipment.status = Equipment.Status.MAINTENANCE
        # Keep current_user so ownership is not lost while the item is repaired.
        equipment.save(update_fields=['status'])

    if job.assigned_to_id:
        job.status = MaintenanceJob.Status.ASSIGNED
        job.assigned_at = timezone.now()
        job.save(update_fields=['status', 'assigned_at', 'updated_at'])

    _event(
        job, MaintenanceEvent.Kind.CREATED, actor,
        f'{job.equipment_name} received for maintenance.',
        payload={'token_id': str(token.id)},
    )
    if customer is not None:
        _event(
            job, MaintenanceEvent.Kind.CREATED, actor,
            (f'New customer contact {customer.customer_code} created from intake.'
             if customer_created else
             f'Linked to existing customer {customer.customer_code}.'),
            public=False,
            payload={'customer_id': str(customer.pk), 'created': customer_created},
        )
    if job.assigned_to_id:
        _event(
            job, MaintenanceEvent.Kind.ASSIGNED, actor,
            f'Assigned to {job.assigned_to.full_name}.',
            old_status=MaintenanceJob.Status.INTAKE,
            new_status=MaintenanceJob.Status.ASSIGNED,
        )

    schedule_deadline_reminders(job)
    link = public_tracking_url(job, request=request)
    transaction.on_commit(lambda: notifications.notify_job_created(job, tracking_url=link, actor=actor))
    return job


def _upsert_reminder(job, key, **defaults):
    reminder, _ = MaintenanceReminder.objects.update_or_create(
        job=job,
        key=key,
        defaults={**defaults, 'is_active': True},
    )
    return reminder


def schedule_deadline_reminders(job: MaintenanceJob):
    """Create/update reminders whenever deadlines change."""
    if job.promised_completion_at and not job.is_terminal:
        technician_time = max(timezone.now(), job.promised_completion_at - timedelta(hours=4))
        _upsert_reminder(
            job, 'technician_completion_deadline',
            target=MaintenanceReminder.Target.TECHNICIAN,
            channel=MaintenanceReminder.Channel.BOTH,
            subject=f'{job.maintenance_number}: completion deadline approaching',
            message=(
                f'{job.equipment_name} is due by '
                f'{timezone.localtime(job.promised_completion_at):%d %b %Y %H:%M}.'
            ),
            next_send_at=technician_time,
            repeat_every_hours=4,
            max_sends=4,
        )
        _upsert_reminder(
            job, 'it_completion_overdue',
            target=MaintenanceReminder.Target.IT,
            channel=MaintenanceReminder.Channel.IN_APP,
            subject=f'Overdue maintenance: {job.maintenance_number}',
            message=f'{job.equipment_name} has passed its promised completion deadline.',
            next_send_at=job.promised_completion_at,
            repeat_every_hours=12,
            max_sends=10,
        )

    if job.status == MaintenanceJob.Status.READY_PICKUP and job.pickup_deadline_at:
        _upsert_reminder(
            job, 'owner_pickup',
            target=MaintenanceReminder.Target.OWNER,
            channel=MaintenanceReminder.Channel.EMAIL,
            subject=f'{job.maintenance_number}: equipment ready for pickup',
            message=(
                f'{job.equipment_name} is ready for pickup. Please collect it by '
                f'{timezone.localtime(job.pickup_deadline_at):%d %b %Y %H:%M}.'
            ),
            next_send_at=min(timezone.now() + timedelta(hours=24), job.pickup_deadline_at),
            repeat_every_hours=48,
            max_sends=8,
        )
        _upsert_reminder(
            job, 'it_pickup_overdue',
            target=MaintenanceReminder.Target.IT,
            channel=MaintenanceReminder.Channel.IN_APP,
            subject=f'Pickup overdue: {job.maintenance_number}',
            message=f'{job.owner_display_name} has not collected {job.equipment_name}.',
            next_send_at=job.pickup_deadline_at,
            repeat_every_hours=24,
            max_sends=14,
        )


def cancel_irrelevant_reminders(job: MaintenanceJob):
    if job.status in {
        MaintenanceJob.Status.READY_PICKUP,
        MaintenanceJob.Status.COLLECTED,
        MaintenanceJob.Status.CLOSED,
        MaintenanceJob.Status.CANCELLED,
        MaintenanceJob.Status.UNREPAIRABLE,
    }:
        job.reminders.filter(key__in=[
            'technician_completion_deadline', 'it_completion_overdue'
        ]).update(is_active=False)
    if job.status in {
        MaintenanceJob.Status.COLLECTED,
        MaintenanceJob.Status.CLOSED,
        MaintenanceJob.Status.CANCELLED,
    }:
        job.reminders.filter(key__in=['owner_pickup', 'it_pickup_overdue']).update(is_active=False)


@transaction.atomic
def assign_job(job: MaintenanceJob, technician, *, actor) -> MaintenanceJob:
    job = MaintenanceJob.objects.select_for_update().get(pk=job.pk)
    job.assigned_to = technician
    job.assigned_at = timezone.now()
    old = job.status
    if job.status == MaintenanceJob.Status.INTAKE:
        job.status = MaintenanceJob.Status.ASSIGNED
    job.save(update_fields=['assigned_to', 'assigned_at', 'status', 'updated_at'])
    _event(
        job, MaintenanceEvent.Kind.ASSIGNED, actor,
        f'Assigned to {technician.full_name}.',
        old_status=old, new_status=job.status,
    )
    transaction.on_commit(lambda: notifications.notify_assignment(job, actor=actor))
    return job


@transaction.atomic
def transition_job(job: MaintenanceJob, new_status: str, *, actor=None, note='', public_note='') -> MaintenanceJob:
    job = MaintenanceJob.objects.select_for_update().select_related('equipment').get(pk=job.pk)
    old_status = job.status
    if new_status == old_status:
        return job
    if new_status not in ALLOWED_TRANSITIONS.get(old_status, set()):
        raise ValueError(
            f'Cannot move from {job.get_status_display()} to '
            f'{dict(MaintenanceJob.Status.choices).get(new_status, new_status)}.'
        )

    now = timezone.now()
    update_fields = ['status', 'updated_at']
    job.status = new_status

    if new_status == MaintenanceJob.Status.DIAGNOSIS and not job.assigned_at:
        job.assigned_at = now
        update_fields.append('assigned_at')
    elif new_status == MaintenanceJob.Status.READY_PICKUP:
        job.completed_at = job.completed_at or now
        job.ready_at = now
        job.pickup_deadline_at = job.pickup_deadline_at or (now + timedelta(days=7))
        update_fields.extend(['completed_at', 'ready_at', 'pickup_deadline_at'])
    elif new_status == MaintenanceJob.Status.COLLECTED:
        job.collected_at = now
        update_fields.append('collected_at')
    elif new_status == MaintenanceJob.Status.CLOSED:
        if job.total_amount > 0 and job.payment_status != MaintenanceJob.PaymentStatus.PAID:
            raise ValueError('Record full payment before closing this paid maintenance job.')
        job.closed_at = now
        update_fields.append('closed_at')
        if job.warranty_days and not job.warranty_until:
            job.warranty_until = timezone.localdate() + timedelta(days=job.warranty_days)
            update_fields.append('warranty_until')

    if public_note:
        job.owner_visible_note = public_note
        update_fields.append('owner_visible_note')
    if note:
        job.internal_note = note
        update_fields.append('internal_note')
    job.save(update_fields=list(dict.fromkeys(update_fields)))

    if new_status == MaintenanceJob.Status.READY_PICKUP and not job.invoice_id:
        create_invoice_for_job(job, actor=actor, finalize=True)
        job.refresh_from_db()

    if new_status == MaintenanceJob.Status.CLOSED:
        if job.payment_status in {
            MaintenanceJob.PaymentStatus.PAID,
            MaintenanceJob.PaymentStatus.NOT_REQUIRED,
        } and not job.receipt_id:
            create_receipt_for_job(job, actor=actor, finalize=True)
            job.refresh_from_db()

        # Restore the asset to its issued/available state without losing owner.
        if job.equipment_id:
            job.equipment.status = (
                Equipment.Status.ISSUED if job.equipment.current_user_id
                else Equipment.Status.AVAILABLE
            )
            job.equipment.save(update_fields=['status'])

    _event(
        job, MaintenanceEvent.Kind.STATUS, actor,
        public_note or note or f'Status changed to {job.get_status_display()}.',
        old_status=old_status, new_status=new_status,
        public=True,
    )

    schedule_deadline_reminders(job)
    cancel_irrelevant_reminders(job)

    if new_status == MaintenanceJob.Status.CLOSED:
        token = MaintenanceAccessToken.objects.filter(job=job).first()
        # Send final documents before invalidating the public link.
        transaction.on_commit(lambda: notifications.notify_job_closed(job, actor=actor))
        if token:
            token.revoke()
            _event(
                job, MaintenanceEvent.Kind.TOKEN, actor,
                'Owner tracking link closed automatically.', public=False
            )
    elif new_status == MaintenanceJob.Status.READY_PICKUP:
        transaction.on_commit(lambda: notifications.notify_ready_for_pickup(job, actor=actor))
    else:
        transaction.on_commit(lambda: notifications.notify_status_changed(job, actor=actor))

    return job


@transaction.atomic
def customer_quote_response(job: MaintenanceJob, *, approved: bool, note='') -> MaintenanceJob:
    job = MaintenanceJob.objects.select_for_update().get(pk=job.pk)
    if job.status != MaintenanceJob.Status.AWAITING_APPROVAL:
        raise ValueError('This quote is no longer awaiting approval.')

    now = timezone.now()
    if approved:
        job.quote_approved_at = now
        job.quote_declined_at = None
        job.quote_response_note = note
        job.save(update_fields=[
            'quote_approved_at', 'quote_declined_at',
            'quote_response_note', 'updated_at'
        ])
        _event(
            job, MaintenanceEvent.Kind.APPROVAL, None,
            f'Customer approved the repair quote. {note}'.strip(), public=True
        )
        return transition_job(
            job, MaintenanceJob.Status.IN_REPAIR,
            actor=None, public_note='Repair quote approved. Work may proceed.'
        )

    job.quote_declined_at = now
    job.quote_approved_at = None
    job.quote_response_note = note
    job.save(update_fields=[
        'quote_declined_at', 'quote_approved_at',
        'quote_response_note', 'updated_at'
    ])
    _event(
        job, MaintenanceEvent.Kind.APPROVAL, None,
        f'Customer declined the repair quote. {note}'.strip(), public=True
    )
    return transition_job(
        job, MaintenanceJob.Status.CANCELLED,
        actor=None, public_note='Repair quote declined by the customer.'
    )


@transaction.atomic
def add_charge_line(job: MaintenanceJob, *, actor, data: dict) -> MaintenanceChargeLine:
    line = MaintenanceChargeLine.objects.create(
        job=job,
        position=job.charge_lines.count(),
        charge_type=data.get('charge_type') or MaintenanceChargeLine.ChargeType.SERVICE,
        description=(data.get('description') or '').strip(),
        quantity=_decimal(data.get('quantity'), '1'),
        unit_price=_decimal(data.get('unit_price')),
        billable=bool(data.get('billable', True)),
        covered_by_warranty=bool(data.get('covered_by_warranty')),
    )
    if not line.description:
        raise ValueError('Charge description is required.')
    _event(
        job, MaintenanceEvent.Kind.NOTE, actor,
        f'Charge added: {line.description} — {line.line_total} {job.currency}.',
        public=job.status == MaintenanceJob.Status.AWAITING_APPROVAL,
    )
    return line


@transaction.atomic
def update_deadlines(job: MaintenanceJob, *, actor, diagnosis_due_at=None,
                     promised_completion_at=None, pickup_deadline_at=None):
    fields = ['updated_at']
    if diagnosis_due_at is not None:
        job.diagnosis_due_at = diagnosis_due_at
        fields.append('diagnosis_due_at')
    if promised_completion_at is not None:
        job.promised_completion_at = promised_completion_at
        fields.append('promised_completion_at')
    if pickup_deadline_at is not None:
        job.pickup_deadline_at = pickup_deadline_at
        fields.append('pickup_deadline_at')
    job.save(update_fields=fields)
    _event(
        job, MaintenanceEvent.Kind.DEADLINE, actor,
        'Maintenance deadlines updated.',
        public=True,
        payload={
            'diagnosis_due_at': str(job.diagnosis_due_at or ''),
            'promised_completion_at': str(job.promised_completion_at or ''),
            'pickup_deadline_at': str(job.pickup_deadline_at or ''),
        },
    )
    schedule_deadline_reminders(job)
    return job


@transaction.atomic
def record_payment(job: MaintenanceJob, *, actor, amount, method='', reference='',
                   allow_overpayment=False):
    job = MaintenanceJob.objects.select_for_update().get(pk=job.pk)
    amount = _decimal(amount)
    if amount <= 0:
        raise ValueError('Payment amount must be greater than zero.')

    # Reject overpayment rather than silently absorbing it. The old code did
    # min(total, paid + amount), so handing over 5000 on a 3000 invoice
    # recorded 3000 and the customer's receipt under-reported what they
    # actually paid — a real dispute waiting to happen. Surface it instead
    # and let the desk decide (change due, or an explicit override).
    new_total_paid = job.amount_paid + amount
    if new_total_paid > job.total_amount and not allow_overpayment:
        excess = new_total_paid - job.total_amount
        raise ValueError(
            f'Payment of {amount} {job.currency} exceeds the outstanding '
            f'balance of {job.balance_due} {job.currency} by {excess}. '
            f'Collect the balance exactly, or confirm the overpayment.'
        )

    job.amount_paid = new_total_paid
    job.payment_method = (method or '').strip()
    job.payment_reference = (reference or '').strip()
    job.paid_at = timezone.now()
    if job.amount_paid >= job.total_amount:
        job.payment_status = MaintenanceJob.PaymentStatus.PAID
    else:
        job.payment_status = MaintenanceJob.PaymentStatus.PARTIAL
    job.save(update_fields=[
        'amount_paid', 'payment_method', 'payment_reference',
        'paid_at', 'payment_status', 'updated_at'
    ])
    _event(
        job, MaintenanceEvent.Kind.PAYMENT, actor,
        f'Payment recorded: {amount} {job.currency} by {job.payment_method or "unspecified method"}.',
        public=True,
        payload={'amount': str(amount), 'reference': job.payment_reference},
    )
    if job.payment_status == MaintenanceJob.PaymentStatus.PAID and not job.receipt_id:
        create_receipt_for_job(job, actor=actor, finalize=True)
        job.refresh_from_db()
        transaction.on_commit(lambda: notifications.notify_receipt_ready(job, actor=actor))
    return job


def _default_letterhead(actor):
    from apps.files.models import SharedFile
    from apps.invoices.models import InvoiceTemplate

    configured = getattr(settings, 'IT_MAINTENANCE_INVOICE_LETTERHEAD_ID', None)
    if configured:
        letterhead = SharedFile.objects.filter(pk=configured, is_latest=True).first()
        if letterhead:
            return letterhead

    template_name = getattr(settings, 'IT_MAINTENANCE_INVOICE_TEMPLATE_NAME', 'Maintenance')
    template = InvoiceTemplate.objects.filter(name__iexact=template_name).select_related('letterhead').first()
    if template:
        return template.letterhead

    letterhead = SharedFile.objects.filter(
        is_latest=True, name__iendswith='.pdf'
    ).order_by('-created_at').first()
    if letterhead:
        return letterhead

    raise ImproperlyConfigured(
        'No PDF letterhead is available. Set IT_MAINTENANCE_INVOICE_LETTERHEAD_ID '
        'or create an invoice template named "Maintenance".'
    )


def _finalize_invoice_document(document, actor):
    from apps.invoices.services import finalize_invoice
    return finalize_invoice(document, actor)


@transaction.atomic
def create_invoice_for_job(job: MaintenanceJob, *, actor, finalize=True):
    if job.invoice_id:
        return job.invoice
    if not job.charge_lines.exists():
        MaintenanceChargeLine.objects.create(
            job=job,
            position=0,
            charge_type=MaintenanceChargeLine.ChargeType.SERVICE,
            description='Equipment maintenance service — no charge',
            quantity=Decimal('1.00'),
            unit_price=Decimal('0.00'),
            billable=True,
        )
    job.recalculate_totals(save=True)

    from apps.invoices.models import DocType, InvoiceDocument, InvoiceLineItem

    # Prefer the linked contact's address book entry when intake left the
    # address blank — but never override what the desk actually typed.
    client_address = job.owner_address
    if not client_address and job.customer_id:
        client_address = job.customer.address or ''

    invoice = InvoiceDocument.objects.create(
        doc_type=DocType.INVOICE,
        letterhead=_default_letterhead(actor),
        client_name=job.owner_display_name,
        client_address=client_address,
        client_email=job.effective_owner_email,
        po_reference=job.maintenance_number,
        invoice_date=timezone.localdate(),
        due_date=timezone.localdate(),
        payment_terms='Due on pickup',
        currency=job.currency,
        tax_rate=job.tax_rate,
        discount_amount=job.discount_amount,
        notes=(
            f'Equipment maintenance: {job.equipment_name}\n'
            f'Maintenance reference: {job.maintenance_number}\n'
            f'Serial: {job.serial_number or "N/A"}'
        ),
        created_by=actor,
    )
    for idx, line in enumerate(job.charge_lines.filter(billable=True).order_by('position', 'created_at')):
        InvoiceLineItem.objects.create(
            invoice=invoice,
            position=idx,
            description=f'{line.get_charge_type_display()}: {line.description}',
            quantity=line.quantity,
            unit_price=line.unit_price,
        )
    invoice.recalculate_totals(save=True)
    if finalize:
        invoice = _finalize_invoice_document(invoice, actor)

    job.invoice = invoice
    job.save(update_fields=['invoice', 'updated_at'])
    _event(
        job, MaintenanceEvent.Kind.INVOICE, actor,
        f'Invoice {invoice.number or invoice.preview_number} generated.',
        public=True,
        payload={'invoice_id': str(invoice.pk)},
    )
    return invoice


@transaction.atomic
def create_receipt_for_job(job: MaintenanceJob, *, actor, finalize=True):
    if job.receipt_id:
        return job.receipt
    if job.payment_status not in {
        MaintenanceJob.PaymentStatus.PAID,
        MaintenanceJob.PaymentStatus.NOT_REQUIRED,
    }:
        raise ValueError('A receipt can only be generated after full payment or for a no-charge service.')

    # A zero-value receipt is meaningless and the invoice app rejects it
    # (receipt amount must be > 0). A no-charge job's proof of service is its
    # invoice and signed service record, not a receipt for nothing.
    if job.total_amount <= Decimal('0.00') and job.amount_paid <= Decimal('0.00'):
        raise ValueError(
            'This is a no-charge service — there is nothing to receipt. '
            'The zero-value invoice and the printed service record are the '
            'proof of service.'
        )
    if not job.invoice_id:
        create_invoice_for_job(job, actor=actor, finalize=True)
        job.refresh_from_db()

    try:
        from apps.invoices.receipt_services import create_receipt_from_invoice
    except ImportError as exc:
        raise ImproperlyConfigured(
            'Install apps/invoices/receipt_services.py from the included invoice patch.'
        ) from exc

    receipt = create_receipt_from_invoice(
        job.invoice,
        actor=actor,
        amount_paid=job.amount_paid,
        payment_method=job.payment_method,
        payment_reference=job.payment_reference,
        paid_at=job.paid_at,
        notes=f'Maintenance reference: {job.maintenance_number}',
        finalize=finalize,
        # Required for the NOT_REQUIRED (no-charge) path allowed above: a
        # zero-charge job has amount_paid = 0, which is less than the
        # invoice total, and the invoice app rejects a short receipt unless
        # part-payment is explicitly permitted. Without this, every
        # goodwill/warranty repair raises ReceiptError on close.
        allow_partial=True,
    )
    job.receipt = receipt
    job.save(update_fields=['receipt', 'updated_at'])
    _event(
        job, MaintenanceEvent.Kind.RECEIPT, actor,
        f'Receipt {receipt.number or receipt.preview_number} generated.',
        public=True,
        payload={'receipt_id': str(receipt.pk)},
    )
    return receipt


@transaction.atomic
def regenerate_tracking_token(job: MaintenanceJob, *, actor, request=None):
    if job.status == MaintenanceJob.Status.CLOSED:
        raise ValueError('A closed maintenance record cannot receive a new tracking link.')
    token, _ = MaintenanceAccessToken.objects.get_or_create(job=job)
    token.rotate()
    _event(
        job, MaintenanceEvent.Kind.TOKEN, actor,
        'Owner tracking link regenerated. The old link no longer works.', public=False
    )
    link = public_tracking_url(job, request=request)
    transaction.on_commit(lambda: notifications.send_tracking_link(job, link, actor=actor))
    return link
