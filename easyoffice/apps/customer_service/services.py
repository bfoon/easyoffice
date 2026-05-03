"""
apps/customer_service/services.py
─────────────────────────────────
Business logic for tickets, assignments, routing, and ticket→order linkage.

Why this module exists
══════════════════════
The original assignment + routing actions were firing notifications
*inside* the transaction. That has two failure modes:

    1. If the transaction rolls back, the notifications still went out and
       reference rows that no longer exist.
    2. If the notification reads back the assignment / route immediately,
       it can hit a stale read (or, on some DB backends, fail outright).

The fix is the standard Django pattern: do all writes inside an atomic
block, then schedule notifications with `transaction.on_commit(...)`.
That way they only fire if the DB commit succeeds, and they see the final
state of every related row.

This module also adds:
    • `create_order_from_ticket` — turn a call/ticket into a SalesOrder
    • `complete_assignment`      — closes a ServiceTicketAssignment and
                                   notifies the heads + ticket owner
    • `confirm_and_close_ticket` — owner-side confirmation that closes the
                                   ticket only if every assignment is done
"""
from __future__ import annotations

import logging
from datetime import timedelta
from decimal import Decimal
from typing import List, Optional

from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone

from .models import (
    Customer, ServiceTicket, ServiceTicketAssignment,
    ServiceTicketRouting, ServiceTicketUpdate, SLAPolicy,
)
from . import notifications

logger = logging.getLogger(__name__)
User = get_user_model()


# ── Department-name helpers ────────────────────────────────────────────────
#
# We treat anything whose name contains "customer service", "cs", or
# "marketing" as a CS-side department. Routing to one of those does NOT
# create a downstream assignment — the receiving CS team handles it directly.

CS_DEPARTMENT_KEYWORDS = ('customer service', 'cs', 'marketing', 'sales and marketing')


def is_cs_department(department) -> bool:
    if not department:
        return False
    name = (department.name or '').lower()
    return any(kw in name for kw in CS_DEPARTMENT_KEYWORDS)


def _user_department(user):
    """Best-effort: pull the user's department off StaffProfile."""
    if not user:
        return None
    try:
        return user.staffprofile.department
    except Exception:
        return None


def is_cs_user(user) -> bool:
    """A user is 'CS' when their StaffProfile department is a CS-side dept."""
    return is_cs_department(_user_department(user))


# ── Task auto-creation ────────────────────────────────────────────────────
#
# When a ticket is routed/assigned to a NON-CS staff member, the platform
# should also create a row in apps.tasks.Task so the work shows up in that
# user's normal task inbox (not just the CS module). Mirrors the helper in
# views.py so the service layer doesn't depend on the view layer.

_TASK_PRIORITY_MAP = {
    'low':      'LOW',
    'medium':   'MEDIUM',
    'high':     'HIGH',
    'critical': 'CRITICAL',
}


def _ensure_task_category(department):
    if not department:
        return None
    try:
        from apps.tasks.models import TaskCategory
    except Exception:
        return None
    try:
        category, _ = TaskCategory.objects.get_or_create(
            name=f'Customer Service - {department.name}',
            defaults={
                'department': department,
                'icon':       'bi-headset',
                'color':      '#8b5cf6',
            },
        )
        return category
    except Exception:
        logger.exception('Could not get/create TaskCategory for %s', department)
        return None


def _create_task_for_assignment(assignment, *, creator, assignee):
    """
    Create a tasks.Task linked back to the given ServiceTicketAssignment.

    Called only for NON-CS recipients — CS staff use the CS module directly
    and don't need a duplicate row in the tasks app.

    Returns the Task instance, or None if anything goes wrong (the caller
    must not fail because of this — it's a side effect).
    """
    if not assignee:
        return None
    try:
        from apps.tasks.models import Task
    except Exception:
        logger.warning('apps.tasks not installed; skipping task auto-create.')
        return None

    if assignment.task_ref_id:
        return assignment.task_ref  # idempotent

    try:
        category = _ensure_task_category(assignment.department)
        priority_name = _TASK_PRIORITY_MAP.get(
            (assignment.ticket.priority or 'medium'), 'MEDIUM',
        )
        priority_value = getattr(Task.Priority, priority_name, None) or getattr(
            Task.Priority, 'MEDIUM',
        )

        task = Task.objects.create(
            title=assignment.title or f'Ticket {assignment.ticket.ticket_no}',
            description=(
                assignment.instructions
                or f'Customer service work item for {assignment.ticket.ticket_no}: '
                   f'{assignment.ticket.subject}'
            ),
            assigned_to=assignee,
            assigned_by=creator,
            category=category,
            due_date=assignment.sla_due_at,
            priority=priority_value,
        )
        assignment.task_ref = task
        assignment.save(update_fields=['task_ref', 'updated_at'])
        return task
    except Exception:
        logger.exception(
            'Failed to auto-create Task for assignment %s', assignment.pk,
        )
        return None


# ── SLA helpers ────────────────────────────────────────────────────────────

def _resolve_sla_due(ticket: ServiceTicket, department) -> Optional[timezone.datetime]:
    """
    Look up an active SLAPolicy for (department, ticket_type, priority)
    and return the calculated due-time. Falls back to a sane default of
    24 hours if no policy is on file.
    """
    if not department:
        return timezone.now() + timedelta(hours=24)
    try:
        policy = SLAPolicy.objects.filter(
            department=department,
            request_type=ticket.ticket_type,
            priority=ticket.priority,
            is_active=True,
        ).first()
    except Exception:
        policy = None
    hours = policy.resolution_hours if policy else 24
    return timezone.now() + timedelta(hours=hours)


# ════════════════════════════════════════════════════════════════════════
# ASSIGNMENT
# ════════════════════════════════════════════════════════════════════════

@transaction.atomic
def assign_ticket_to_user(
    ticket: ServiceTicket,
    *,
    assigned_to,
    actor,
    title: str = '',
    instructions: str = '',
    department=None,
) -> ServiceTicketAssignment:
    """
    Assign a ticket to a single user. Notifies the assignee + head of CS
    after the transaction commits.

    `department` defaults to the assignee's StaffProfile department, then
    the ticket's current department.
    """
    if not assigned_to:
        raise ValueError('An assignee is required.')

    # Lock the ticket row to avoid two simultaneous assigns
    ticket = ServiceTicket.objects.select_for_update().get(pk=ticket.pk)

    if not department:
        try:
            department = assigned_to.staffprofile.department
        except Exception:
            department = ticket.department

    sla_due = _resolve_sla_due(ticket, department)

    assignment = ServiceTicketAssignment.objects.create(
        ticket=ticket,
        department=department,
        assigned_by=actor,
        assigned_to=assigned_to,
        title=title or f'Handle ticket {ticket.ticket_no}',
        instructions=instructions or '',
        status='assigned',
        sla_due_at=sla_due,
    )

    # Bump ticket state
    update_fields = []
    if ticket.status in ('new', 'open'):
        ticket.status = 'assigned'
        update_fields.append('status')
    if not ticket.current_owner_id:
        ticket.current_owner = assigned_to
        update_fields.append('current_owner')
    if department and ticket.department_id != department.pk:
        ticket.department = department
        update_fields.append('department')
    if update_fields:
        update_fields.append('updated_at')
        ticket.save(update_fields=update_fields)

    ServiceTicketUpdate.objects.create(
        ticket=ticket, user=actor, update_type='note',
        body=(
            f'Assigned to {notifications._user_full_name(assigned_to)}'
            + (f' — {instructions}' if instructions else '.')
        ),
    )

    # If the assignee is NOT part of customer service, mirror this assignment
    # into apps.tasks.Task so it appears in their normal task inbox. CS staff
    # work directly inside the CS module and don't need a duplicated row.
    if not is_cs_user(assigned_to):
        _create_task_for_assignment(assignment, creator=actor, assignee=assigned_to)

    transaction.on_commit(
        lambda: _safe(notifications.notify_assignment_created, assignment, actor=actor)
    )
    return assignment


# ════════════════════════════════════════════════════════════════════════
# ROUTING
# ════════════════════════════════════════════════════════════════════════

@transaction.atomic
def route_ticket_to_department(
    ticket: ServiceTicket,
    *,
    to_department,
    actor,
    note: str = '',
    auto_create_assignment: bool = True,
) -> ServiceTicketRouting:
    """
    Route a ticket to another department.

    • Records a ServiceTicketRouting row
    • Updates ticket.department
    • If the destination is a NON-CS department AND
      auto_create_assignment is True, creates a department-wide
      ServiceTicketAssignment (with no specific assignee — anyone in the
      department can pick it up). This is the "task" that closes when the
      department finishes the work.
    • Notifies every member of the destination department, plus heads.
    """
    if not to_department:
        raise ValueError('A destination department is required.')

    ticket = ServiceTicket.objects.select_for_update().get(pk=ticket.pk)
    from_department = ticket.department

    if from_department and from_department.pk == to_department.pk:
        raise ValueError('Ticket is already assigned to that department.')

    routing = ServiceTicketRouting.objects.create(
        ticket=ticket,
        action='route',
        from_department=from_department,
        to_department=to_department,
        from_user=actor,
        note=note or '',
    )

    # Update ticket department + status
    ticket.department = to_department
    if ticket.status in ('new', 'open'):
        ticket.status = 'open'
    ticket.save(update_fields=['department', 'status', 'updated_at'])

    ServiceTicketUpdate.objects.create(
        ticket=ticket, user=actor, update_type='note',
        body=(
            f'Routed to {to_department.name}'
            + (f' — {note}' if note else '.')
        ),
    )

    # Auto-create department-level assignment when leaving CS
    created_assignment = None
    if auto_create_assignment and not is_cs_department(to_department):
        sla_due = _resolve_sla_due(ticket, to_department)

        # Target the department head if there is one. The head sees it in
        # their personal task inbox; if there's no head on file we fall
        # back to a department-wide assignment that anyone on the team
        # can pick up.
        dept_head = getattr(to_department, 'head', None)

        created_assignment = ServiceTicketAssignment.objects.create(
            ticket=ticket,
            department=to_department,
            assigned_by=actor,
            assigned_to=dept_head,  # may be None — that's OK
            title=f'Handle routed ticket {ticket.ticket_no}',
            instructions=note or '',
            status='assigned' if dept_head else 'new',
            sla_due_at=sla_due,
        )

        # Mirror into apps.tasks for the receiving (non-CS) head so the
        # work surfaces in their regular task list, not only inside CS.
        if dept_head and not is_cs_user(dept_head):
            _create_task_for_assignment(
                created_assignment, creator=actor, assignee=dept_head,
            )

    # Fire notifications AFTER commit
    def _after_commit():
        _safe(
            notifications.notify_route_to_department,
            ticket,
            to_department=to_department,
            from_department=from_department,
            actor=actor,
            note=note,
        )
        if created_assignment:
            _safe(
                notifications.notify_assignment_created,
                created_assignment,
                actor=actor,
            )
    transaction.on_commit(_after_commit)

    return routing


# ════════════════════════════════════════════════════════════════════════
# ASSIGNMENT COMPLETION  (department finishes the work)
# ════════════════════════════════════════════════════════════════════════

@transaction.atomic
def complete_assignment(
    assignment: ServiceTicketAssignment,
    *,
    actor,
    resolution_note: str = '',
    final_status: str = 'resolved',
) -> ServiceTicketAssignment:
    """
    Mark an assignment complete. Notifies:
        • Head of CS
        • Head of the assignment's department
        • The ticket creator + current owner (so they can confirm + close)

    The ticket itself is NOT auto-closed — the CS-side owner has to confirm.
    This is what your spec asks for: "the CS head and department will be
    notified and the ticket creator or owner can confirm and the ticket
    will close."
    """
    if final_status not in ('resolved', 'closed'):
        raise ValueError("final_status must be 'resolved' or 'closed'.")

    assignment = ServiceTicketAssignment.objects.select_for_update().get(pk=assignment.pk)
    if assignment.status in ('resolved', 'closed'):
        return assignment

    assignment.status = final_status
    assignment.completed_at = timezone.now()
    assignment.save(update_fields=['status', 'completed_at', 'updated_at'])

    ticket = assignment.ticket
    ServiceTicketUpdate.objects.create(
        ticket=ticket,
        user=actor,
        update_type='resolution' if final_status == 'resolved' else 'status_change',
        body=(
            f'Assignment "{assignment.title}" marked {final_status} '
            f'by {notifications._user_full_name(actor)}.'
            + (f'\n\n{resolution_note}' if resolution_note else '')
        ),
    )

    # Are all assignments now done? If so, ping the owner to confirm + close.
    open_count = ticket.assignments.exclude(status__in=('resolved', 'closed')).count()

    transaction.on_commit(
        lambda: _safe(notifications.notify_assignment_completed, assignment, actor=actor)
    )
    if open_count == 0 and ticket.status not in ('resolved', 'closed', 'cancelled'):
        transaction.on_commit(
            lambda: _safe(notifications.notify_ticket_ready_to_close,
                          ticket, assignment=assignment, actor=actor)
        )

    return assignment


# ════════════════════════════════════════════════════════════════════════
# OWNER-SIDE CONFIRMATION → CLOSE TICKET
# ════════════════════════════════════════════════════════════════════════

@transaction.atomic
def confirm_and_close_ticket(
    ticket: ServiceTicket,
    *,
    actor,
    confirmation_note: str = '',
    force: bool = False,
) -> ServiceTicket:
    """
    Called by the ticket creator/owner once the customer has confirmed
    everything is sorted. Refuses to close if any assignment is still open
    (unless `force=True`, used by supervisors).
    """
    ticket = ServiceTicket.objects.select_for_update().get(pk=ticket.pk)

    if ticket.status in ('closed', 'cancelled'):
        return ticket

    if not force:
        open_assignments = ticket.assignments.exclude(
            status__in=('resolved', 'closed')
        ).count()
        if open_assignments:
            raise ValueError(
                f'Cannot close: {open_assignments} assignment(s) still open. '
                f'Wait for the department to complete them, or use force=True.'
            )

    ticket.status = 'closed'
    ticket.closed_at = timezone.now()
    if not ticket.resolved_at:
        ticket.resolved_at = ticket.closed_at
    if not ticket.resolved_by_id:
        ticket.resolved_by = actor
    ticket.save(update_fields=[
        'status', 'closed_at', 'resolved_at', 'resolved_by', 'updated_at',
    ])

    ServiceTicketUpdate.objects.create(
        ticket=ticket,
        user=actor,
        update_type='resolution',
        body=(
            f'Ticket confirmed closed by {notifications._user_full_name(actor)}.'
            + (f'\n\n{confirmation_note}' if confirmation_note else '')
        ),
    )

    return ticket


# ════════════════════════════════════════════════════════════════════════
# TICKET → ORDER  (link orders to calls)
# ════════════════════════════════════════════════════════════════════════

@transaction.atomic
def create_order_from_ticket(
    ticket: ServiceTicket,
    *,
    actor,
    items: List[dict],
    contact_overrides: Optional[dict] = None,
    delivery_address: str = '',
    notes: str = '',
    currency: str = 'GMD',
    tax_rate: Decimal = Decimal('0'),
    discount_amount: Decimal = Decimal('0'),
):
    """
    Create a SalesOrder from a customer-service ticket. Pre-fills the order
    payload from the ticket's customer + call so the agent doesn't have to
    re-type anything.

    Used in three places:
        • Call Desk — at the end of a call, "Create order from this call"
        • Ticket detail — "Create order" action
        • Customer detail — "New order for this customer"
    """
    # Lazy import — orders is a separate app
    try:
        from apps.orders import services as orders_services
        from apps.orders.models import OrderSource
    except Exception as exc:
        raise RuntimeError(
            'Orders app is not available. Make sure apps.orders is installed.'
        ) from exc

    if not items:
        raise ValueError('Cannot create an order without at least one item.')

    customer = ticket.customer
    overrides = contact_overrides or {}

    payload = {
        'customer_id':      customer.pk if customer else None,
        'contact_name':     overrides.get('contact_name')
                            or (customer.display_name if customer else ''),
        'contact_phone':    overrides.get('contact_phone')
                            or (ticket.callback_number or ''),
        'contact_email':    overrides.get('contact_email')
                            or (ticket.callback_email or
                                (customer.email if customer else '')),
        'delivery_address': delivery_address
                            or (customer.address if customer else ''),
        'notes':            notes
                            or f'From ticket {ticket.ticket_no}: {ticket.subject}',
        'currency':         currency,
        'tax_rate':         tax_rate,
        'discount_amount':  discount_amount,
        'external_ref':     ticket.ticket_no,
        'items':            items,
    }

    # Map ticket source → order source
    source = OrderSource.PHONE
    if ticket.call_id:
        source = OrderSource.PHONE  # came from a call
    elif ticket.ticket_type == 'sales':
        source = OrderSource.WALK_IN

    order = orders_services.create_order_from_payload(
        payload, source=source, actor=actor,
    )

    # Cross-link: orders.SalesOrder.ticket already exists (FK in your model)
    if not order.ticket_id:
        order.ticket = ticket
        order.save(update_fields=['ticket', 'updated_at'])

    ServiceTicketUpdate.objects.create(
        ticket=ticket, user=actor, update_type='note',
        body=(
            f'Sales order {order.order_no} created from this ticket '
            f'(total: {order.currency} {order.total}).'
        ),
    )

    transaction.on_commit(
        lambda: _safe(notifications.notify_order_created_from_ticket,
                      ticket, order, actor=actor)
    )
    return order


# ════════════════════════════════════════════════════════════════════════
# Helpers
# ════════════════════════════════════════════════════════════════════════

def _safe(fn, *args, **kwargs) -> None:
    """Run a notification fn, swallow + log any exception."""
    try:
        fn(*args, **kwargs)
    except Exception:
        logger.exception('Notification %s raised', fn.__name__)