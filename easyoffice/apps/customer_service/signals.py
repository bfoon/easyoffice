"""
apps/customer_service/signals.py
─────────────────────────────────
Cross-cutting side-effects for customer-service models.

Why a signal instead of inline code?
    The codebase has several places that create a ServiceTicketAssignment:
        • apps.customer_service.services.assign_ticket_to_user
        • apps.customer_service.services.route_ticket_to_department
        • apps.customer_service.views.CallDeskView   (call-desk flow)
        • apps.customer_service.views.TicketActionView   (assign action)
        • Django admin
        • future REST endpoints

    Putting the "if assignee is non-CS, also create a tasks.Task" logic
    inline in each path means every new code path becomes a place for
    the rule to silently break. A post_save signal catches them all.

The handler is idempotent: if `task_ref` is already set on the row, it
does nothing. So even if a service-layer call ALSO created a task, we
won't double up.
"""
from __future__ import annotations

import logging

from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import ServiceTicketAssignment

logger = logging.getLogger(__name__)


@receiver(post_save, sender=ServiceTicketAssignment)
def auto_create_task_for_non_cs_assignee(sender, instance, created, **kwargs):
    """
    When a ServiceTicketAssignment is created and assigned to a non-CS
    user, mirror it into apps.tasks.Task so the work appears in that
    user's normal task inbox.

    Rules:
        • Only fires on the create event (not on subsequent updates).
        • Skips assignments without an `assigned_to` (department-wide ones).
        • Skips assignments where the assignee IS in a CS-side department —
          they already see the work inside the CS module.
        • Skips if `task_ref` is already set (idempotent).
    """
    if not created:
        return
    if not instance.assigned_to_id:
        return
    if instance.task_ref_id:
        return

    # Imported lazily so the signals module loads cleanly even if services
    # is mid-import. services depends only on models, so this is safe.
    from . import services

    if services.is_cs_user(instance.assigned_to):
        return

    try:
        services._create_task_for_assignment(
            instance,
            creator=instance.assigned_by,
            assignee=instance.assigned_to,
        )
    except Exception:
        # Side-effect — must NEVER break the save() that triggered us.
        logger.exception(
            'auto_create_task_for_non_cs_assignee failed for assignment %s',
            instance.pk,
        )
