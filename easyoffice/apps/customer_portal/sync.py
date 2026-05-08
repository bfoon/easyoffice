"""
apps/customer_portal/sync.py
=============================

Bidirectional sync between PortalSupportRequest (customer-facing) and
ServiceTicket (staff-facing).

Two directions, same module:

  ┌──────────────────────────┐         ┌─────────────────────────┐
  │ PortalSupportRequest     │ ◄─────► │ ServiceTicket           │
  │  (customer_portal)       │         │  (customer_service)     │
  │  - status                │         │  - status               │
  │  - TicketComment thread  │         │  - ServiceTicketUpdate  │
  └──────────────────────────┘         └─────────────────────────┘

Customer side                          Staff side
─────────────                          ──────────
  open                  ↔               new, open, pending, assigned
  in_progress           ↔               in_progress
  on_hold               ↔               awaiting_customer
  resolved              ↔               resolved
  closed                ↔               closed
  cancelled             ↔               cancelled
  reopened              →               open
  pending_review        →               new

The mapping is intentionally lossy: the staff side has more granular
states (e.g. 'assigned', 'escalated') that don't change what the
customer sees. We collapse staff states into a small number of customer
states and only push back when the canonical state changes.

Idempotency: every helper here is safe to call multiple times with the
same inputs. We use _SYNC_IN_PROGRESS as a thread-local to avoid the
A→B→A→B ping-pong when a portal change triggers a staff change which
would normally trigger another portal change.
"""
from __future__ import annotations

import logging
import threading
from typing import Optional

from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Re-entrancy guard
#
# When the portal updates ServiceTicket.status, the post_save signal we
# install would normally fire and try to push the change BACK to the portal.
# We use a thread-local to short-circuit that loop.
# ─────────────────────────────────────────────────────────────────────────────

_thread_state = threading.local()


def _is_syncing() -> bool:
    return getattr(_thread_state, 'in_sync', False)


class _SyncContext:
    """Context manager that suppresses re-entrant sync."""
    def __enter__(self):
        _thread_state.in_sync = True
        return self

    def __exit__(self, exc_type, exc, tb):
        _thread_state.in_sync = False
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Status maps
# ─────────────────────────────────────────────────────────────────────────────

# Staff status → portal status. Staff has many fine-grained states; we
# collapse "in motion" states under 'in_progress', "awaiting customer" under
# 'on_hold', and so on.
STAFF_TO_PORTAL = {
    'new':                'open',
    'open':               'open',
    'pending':            'open',
    'assigned':           'in_progress',
    'in_progress':        'in_progress',
    'awaiting_customer':  'on_hold',
    'escalated':          'in_progress',
    'resolved':           'resolved',
    'closed':             'closed',
    'cancelled':          'cancelled',
}

# Portal status → staff status. Used when the customer side is the
# initiator (cancel, reopen request).
PORTAL_TO_STAFF = {
    'pending_review': 'new',
    'open':           'open',
    'in_progress':    'in_progress',
    'on_hold':        'awaiting_customer',
    'resolved':       'resolved',
    'closed':         'closed',
    'cancelled':      'cancelled',
    'reopened':       'open',
}


# ─────────────────────────────────────────────────────────────────────────────
# Staff → Portal: when the staff-side ticket changes, mirror to portal
# ─────────────────────────────────────────────────────────────────────────────

def push_staff_status_to_portal(service_ticket) -> Optional[str]:
    """
    Called from the post_save signal on ServiceTicket. Looks up the linked
    PortalSupportRequest and updates its status if appropriate.

    Returns the new portal status if a change was applied, else None.
    """
    if _is_syncing():
        return None

    # Lazy import — these models live in our own app but importing at the
    # top of sync.py would cause AppRegistryNotReady when settings are still
    # loading.
    from .models import PortalSupportRequest, TicketComment

    portal_req = (
        PortalSupportRequest.objects
        .filter(service_ticket=service_ticket)
        .first()
    )
    if not portal_req:
        return None

    new_staff_status = (service_ticket.status or '').lower()
    target_portal_status = STAFF_TO_PORTAL.get(new_staff_status)

    if not target_portal_status:
        return None

    # Don't downgrade portal-only states. If the customer cancelled their
    # ticket and the staff later marks the staff-side ticket as 'closed',
    # we should NOT flip portal back to 'closed' — it stays 'cancelled'.
    if portal_req.status in ('cancelled',) and target_portal_status != 'cancelled':
        return None

    # Don't churn updates if the status hasn't actually changed.
    if portal_req.status == target_portal_status:
        return None

    update_fields = ['status', 'updated_at']
    portal_req.status = target_portal_status
    now = timezone.now()

    if target_portal_status == 'resolved' and not portal_req.resolved_at:
        portal_req.resolved_at = now
        update_fields.append('resolved_at')
    elif target_portal_status == 'closed' and not portal_req.closed_at:
        portal_req.closed_at = now
        update_fields.append('closed_at')
    elif target_portal_status == 'cancelled' and not portal_req.cancelled_at:
        portal_req.cancelled_at = now
        update_fields.append('cancelled_at')

    # Refresh cached agent / technician names so the customer sees the
    # current owner the moment staff progress the ticket.
    try:
        owner = (
            getattr(service_ticket, 'current_owner', None)
            or getattr(service_ticket, 'assigned_to', None)
        )
        if owner:
            new_name = (
                getattr(owner, 'full_name', None)
                or getattr(owner, 'get_full_name', lambda: '')()
                or owner.get_username()
            )
            if new_name and new_name != portal_req.assigned_agent_name:
                portal_req.assigned_agent_name = new_name[:180]
                update_fields.append('assigned_agent_name')
    except Exception:
        logger.warning('customer_portal.sync: agent name refresh failed', exc_info=True)

    with _SyncContext():
        portal_req.save(update_fields=update_fields)

        # Drop a system comment on the customer-visible thread so the
        # transition shows up in the conversation, not just as a colour
        # change on the status pill.
        _post_system_comment_for_status_change(
            portal_req, target_portal_status, source='staff',
        )

    return target_portal_status


def _post_system_comment_for_status_change(portal_req, new_status: str, *, source: str) -> None:
    """Emit a TicketComment(author_kind='system') describing the change."""
    from .models import TicketComment

    label_map = {
        'open':        'Ticket opened — our team is on it.',
        'in_progress': 'Work is in progress on your ticket.',
        'on_hold':     'Ticket is on hold while we wait for more information.',
        'resolved':    'Your ticket has been marked resolved. Please review and let us know if it\'s been fully addressed — you can rate the resolution or request a reopen below.',
        'closed':      'Ticket closed.',
        'cancelled':   'Ticket cancelled.',
    }
    body = label_map.get(new_status)
    if not body:
        return

    TicketComment.objects.create(
        request=portal_req,
        author_kind='system',
        author_display_name='System',
        body=body,
        event_tag=f'status_{new_status}',
    )


# ─────────────────────────────────────────────────────────────────────────────
# Portal → Staff: when the portal initiates a change, mirror to staff
# ─────────────────────────────────────────────────────────────────────────────

def push_portal_status_to_staff(portal_req, *, reason: str = '', actor=None) -> bool:
    """
    Apply portal_req.status to its linked ServiceTicket, AND drop a
    ServiceTicketUpdate row so the change appears in the staff activity
    log. Safe to call even if there's no linked ticket.

    Returns True if the staff ticket was updated.
    """
    if _is_syncing():
        return False

    st = portal_req.service_ticket
    if not st:
        return False

    target_staff_status = PORTAL_TO_STAFF.get(portal_req.status)
    if not target_staff_status:
        return False

    changed = False
    update_fields = []

    if (st.status or '').lower() != target_staff_status:
        st.status = target_staff_status
        update_fields.append('status')
        changed = True

    # Stamp the lifecycle dates the staff model exposes so reports work.
    now = timezone.now()
    if portal_req.status == 'cancelled' and not getattr(st, 'closed_at', None):
        st.closed_at = now
        update_fields.append('closed_at')
        changed = True
    elif portal_req.status == 'resolved' and not getattr(st, 'resolved_at', None):
        st.resolved_at = now
        update_fields.append('resolved_at')
        changed = True
    elif portal_req.status == 'closed' and not getattr(st, 'closed_at', None):
        st.closed_at = now
        update_fields.append('closed_at')
        changed = True

    if not changed:
        return False

    update_fields.append('updated_at')
    with _SyncContext():
        st.save(update_fields=update_fields)
        _log_to_staff_activity(
            st,
            update_type=_status_to_update_type(portal_req.status),
            body=_compose_activity_body(portal_req, reason),
            user=actor,
        )

    return True


def _status_to_update_type(portal_status: str) -> str:
    return {
        'cancelled':  'status_change',
        'resolved':   'resolution',
        'closed':     'status_change',
        'reopened':   'status_change',
    }.get(portal_status, 'customer_update')


def _compose_activity_body(portal_req, reason: str) -> str:
    """Friendly phrasing for the staff activity-log entry."""
    contact_name = getattr(portal_req.contact, 'full_name', '') or 'the customer'

    templates = {
        'cancelled': (
            f'❌ Customer cancelled the ticket via the portal.'
            + (f'\n\nReason: {reason}' if reason else '')
        ),
        'resolved': f'✓ Customer marked the ticket resolved via the portal.',
        'closed':   f'✓ Customer rated and closed the ticket via the portal.',
        'reopened': (
            f'↺ Customer requested a reopen via the portal.'
            + (f'\n\nReason: {reason}' if reason else '')
        ),
    }

    return templates.get(
        portal_req.status,
        f'{contact_name} updated the ticket via the portal (status now: {portal_req.status}).',
    )


def _log_to_staff_activity(service_ticket, *, update_type: str, body: str, user=None) -> None:
    """Drop a ServiceTicketUpdate row. Best-effort; never raises."""
    try:
        from apps.customer_service.models import ServiceTicketUpdate
        ServiceTicketUpdate.objects.create(
            ticket=service_ticket,
            user=user,
            update_type=update_type,
            body=body,
        )
    except Exception:
        logger.exception(
            'customer_portal.sync: failed to write activity entry to staff side '
            '(ticket=%s)', getattr(service_ticket, 'pk', '?'),
        )


# ─────────────────────────────────────────────────────────────────────────────
# Comment mirroring
# ─────────────────────────────────────────────────────────────────────────────

def mirror_customer_comment_to_staff(comment) -> None:
    """
    Customer just posted a TicketComment. Reflect it in the staff activity
    log so agents working on the staff ticket detail see it without having
    to open the portal.
    """
    if _is_syncing():
        return

    portal_req = comment.request
    st = portal_req.service_ticket
    if not st:
        return

    name = comment.author_display_name or getattr(portal_req.contact, 'full_name', 'Customer')
    body = f'💬 Reply from {name} (via portal):\n\n{comment.body}'

    attachments = list(comment.attachments.all()) if comment.id else []
    if attachments:
        body += '\n\nAttachments:\n' + '\n'.join(
            f'  • {a.original_name} ({a.size_human})'
            for a in attachments
        )

    with _SyncContext():
        _log_to_staff_activity(
            st, update_type='customer_update', body=body, user=None,
        )


def mirror_staff_reply_to_portal(*, portal_request, staff_user, body: str):
    """
    Staff agent replied via the staff-side portal-section UI. Create a
    customer-visible TicketComment so it lands in the portal conversation.

    Returns the created TicketComment.
    """
    from .models import TicketComment

    name = (
        getattr(staff_user, 'full_name', None)
        or (staff_user.get_full_name() if hasattr(staff_user, 'get_full_name') else None)
        or staff_user.get_username()
    )

    with _SyncContext():
        comment = TicketComment.objects.create(
            request=portal_request,
            author_kind='staff',
            author_user=staff_user,
            author_display_name=name,
            body=body,
        )

    return comment
