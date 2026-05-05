"""
apps/tasks/views_on_site.py
=============================

NEW MODULE — drop this file alongside your existing apps/tasks/views.py.
Don't merge it in; it's small and self-contained, so keeping it
separate makes the on-site / verification feature easy to maintain.

Wire it into apps/tasks/urls.py with three new URL entries (see
patches/tasks/PATCH_urls.md).

What this module owns:

  * TaskOnSiteView           — POST /tasks/<pk>/on-site/
  * TaskClearOnSiteView      — POST /tasks/<pk>/clear-on-site/
  * TaskMarkDoneView         — POST /tasks/<pk>/mark-done/  (customer-aware)

The third one wraps the existing 'done' flow with the customer-portal
verification logic. The OLD TaskQuickUpdateView still works for
non-customer-facing tasks; this view is the right one to call when the
task is linked to a maintenance visit or service-ticket assignment.
"""
from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db import transaction
from django.http import HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.utils import timezone
from django.views.generic import View

from apps.tasks.models import Task

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _is_admin_user(user):
    return user.is_superuser or user.groups.filter(
        name__in=['CEO', 'Admin', 'Office Manager'],
    ).exists()


def _can_act_on_task(user, task) -> bool:
    """
    Same model as the existing tasks views: assignee, creator, supervisor,
    or admin. Reused here to keep the perm boundary identical.
    """
    if _is_admin_user(user):
        return True
    if task.assigned_to_id == user.id:
        return True
    if task.assigned_by_id == user.id:
        return True
    profile = getattr(task.assigned_to, 'staffprofile', None)
    if profile and profile.supervisor_id == user.id:
        return True
    return False


def _related_assignment(task):
    """
    Find the customer_service.ServiceTicketAssignment that points back to
    this task, if any. The assignment FK on the assignment model is
    `task_ref` (see apps.customer_service.models).
    """
    try:
        from apps.customer_service.models import ServiceTicketAssignment
        return ServiceTicketAssignment.objects.filter(task_ref=task).first()
    except Exception:
        return None


def _related_visit(task):
    """Find the MaintenanceVisit, if any."""
    try:
        from apps.customer_portal.models import MaintenanceVisit
        return MaintenanceVisit.objects.filter(task=task).first()
    except Exception:
        return None


def _contract_for_task(task):
    """
    Resolve which Contract this task is associated with. Order:
      1. The linked MaintenanceVisit's contract.
      2. The linked ticket's contract (if customer_service.ServiceTicket
         has a contract FK from the patch in patches/customer_service).
    """
    visit = _related_visit(task)
    if visit and visit.contract_id:
        return visit.contract

    assn = _related_assignment(task)
    if assn and assn.ticket_id:
        ticket = assn.ticket
        return getattr(ticket, 'contract', None)

    return None


def _contact_emails_for_contract(contract):
    if not contract:
        return []
    try:
        from apps.customer_portal.models import ContractContact
        return list(
            ContractContact.objects
            .filter(contract=contract, is_active=True, receives_pm_notices=True)
            .values_list('email', flat=True)
        )
    except Exception:
        return []


def _gps_from_post(post) -> tuple:
    """Returns (lat, lng) as Decimals or (None, None)."""
    lat_raw = (post.get('gps_latitude') or '').strip()
    lng_raw = (post.get('gps_longitude') or '').strip()
    try:
        lat = Decimal(lat_raw) if lat_raw else None
        lng = Decimal(lng_raw) if lng_raw else None
    except (InvalidOperation, ValueError):
        return None, None
    return lat, lng


# ─────────────────────────────────────────────────────────────────────────────
# Views
# ─────────────────────────────────────────────────────────────────────────────

class TaskOnSiteView(LoginRequiredMixin, View):
    """
    POST /tasks/<pk>/on-site/

    Body fields (all optional):
        gps_latitude, gps_longitude — from navigator.geolocation
        note — free text, e.g. "site reception locked, called Mariama"
    """
    http_method_names = ['post']

    def post(self, request, pk):
        task = get_object_or_404(Task, pk=pk)
        if not _can_act_on_task(request.user, task):
            return HttpResponseForbidden('You cannot mark this task on site.')

        moved = task.mark_on_site(by_user=request.user)
        # Even if not moved (already on site), we still log a fresh event
        # — second arrival the same day is meaningful.
        lat, lng = _gps_from_post(request.POST)
        note = (request.POST.get('note') or '').strip()[:1000]

        contract = _contract_for_task(task)
        assignment = _related_assignment(task)
        visit = _related_visit(task)

        try:
            from apps.customer_portal.models import OnSiteEvent
            event = OnSiteEvent.objects.create(
                task=task,
                assignment=assignment,
                contract=contract,
                event_type='arrive',
                actor_user=request.user,
                gps_latitude=lat,
                gps_longitude=lng,
                note=note,
            )
        except Exception:
            logger.exception('tasks.on_site: could not write OnSiteEvent')
            event = None

        # Mirror to the visit, if any
        if visit:
            try:
                visit.status = 'on_site'
                if not visit.dispatched_at:
                    visit.dispatched_at = timezone.now()
                visit.technician = visit.technician or request.user
                visit.save(update_fields=[
                    'status', 'dispatched_at', 'technician', 'updated_at',
                ])
            except Exception:
                logger.exception('tasks.on_site: visit mirror failed')

        # Fan-out (customer + internal). Best-effort, after commit.
        if event is not None:
            transaction.on_commit(lambda: _fan_out_on_site(event, contract))

        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return JsonResponse({
                'status': 'ok',
                'on_site_at': task.on_site_at.isoformat() if task.on_site_at else None,
                'recorded_event': bool(event),
            })

        if moved:
            messages.success(request, 'Marked on site. Customer and team notified.')
        else:
            messages.info(request, 'Already on site — additional arrival logged.')
        return redirect('task_detail', pk=task.pk)


class TaskClearOnSiteView(LoginRequiredMixin, View):
    """POST /tasks/<pk>/clear-on-site/  — log a 'depart' event."""
    http_method_names = ['post']

    def post(self, request, pk):
        task = get_object_or_404(Task, pk=pk)
        if not _can_act_on_task(request.user, task):
            return HttpResponseForbidden('You cannot clear on-site for this task.')

        task.clear_on_site()

        contract = _contract_for_task(task)
        assignment = _related_assignment(task)

        try:
            from apps.customer_portal.models import OnSiteEvent
            OnSiteEvent.objects.create(
                task=task,
                assignment=assignment,
                contract=contract,
                event_type='depart',
                actor_user=request.user,
            )
        except Exception:
            logger.exception('tasks.clear_on_site: could not write OnSiteEvent')

        messages.success(request, 'Marked as left site.')
        return redirect('task_detail', pk=task.pk)


class TaskMarkDoneView(LoginRequiredMixin, View):
    """
    POST /tasks/<pk>/mark-done/

    Use this for customer-facing tasks (maintenance visits, ticket
    assignments). It's the customer-aware version of TaskQuickUpdateView's
    'done' flow.

    Behaviour:
      * If the task is customer_visible OR linked to a ticket assignment,
        we DO NOT close the ticket immediately. We:
          1. Call task.mark_done_pending_verify()
          2. Notify the assigned CS agent + (optionally) email the
             customer "we're verifying"
          3. Leave the ticket assignment / ticket open until the CS
             agent confirms via the existing ConfirmAndCloseTicketView.
      * If the task has no customer linkage, behave like the existing
        quick-update done flow (just set status=done).
    """
    http_method_names = ['post']

    def post(self, request, pk):
        task = get_object_or_404(Task, pk=pk)
        if not _can_act_on_task(request.user, task):
            return HttpResponseForbidden('You cannot mark this task done.')

        completion_comment = (request.POST.get('completion_comment') or '').strip()
        if not completion_comment:
            messages.error(
                request,
                'Please add a completion comment explaining what was done.',
            )
            return redirect('task_detail', pk=task.pk)

        assignment = _related_assignment(task)
        visit = _related_visit(task)
        is_customer_facing = bool(
            task.customer_visible or assignment or visit,
        )

        if is_customer_facing:
            task.mark_done_pending_verify()
        else:
            # Plain done — same as before
            task.status = task.Status.DONE
            task.completed_at = timezone.now()
            task.progress_pct = 100
            task.save(update_fields=[
                'status', 'completed_at', 'progress_pct', 'updated_at',
            ])

        # Always log a comment so the audit trail is clean
        try:
            from apps.tasks.models import TaskComment
            TaskComment.objects.create(
                task=task,
                author=request.user,
                content=f'✅ Completion note:\n{completion_comment}',
            )
        except Exception:
            logger.exception('tasks.mark_done: could not save completion comment')

        # Mirror to maintenance visit
        if visit:
            try:
                visit.status = 'completed'
                visit.completed_at = timezone.now()
                visit.save(update_fields=['status', 'completed_at', 'updated_at'])
            except Exception:
                logger.exception('tasks.mark_done: visit mirror failed')

        if is_customer_facing:
            contract = _contract_for_task(task)
            cs_owner = _resolve_cs_owner(task, assignment)
            emails = _contact_emails_for_contract(contract)
            transaction.on_commit(
                lambda: _notify_pending_verify(task, cs_owner, emails)
            )
            messages.success(
                request,
                'Marked as done. Customer service will verify with the customer before closing.',
            )
        else:
            messages.success(request, 'Task marked as done.')

        return redirect('task_detail', pk=task.pk)


# ─────────────────────────────────────────────────────────────────────────────
# Notification helpers
# ─────────────────────────────────────────────────────────────────────────────

def _fan_out_on_site(event, contract):
    """Send customer email + in-app to CEO/Admin/Sales heads."""
    try:
        from apps.customer_portal import notifications as cp_notifications
        from apps.customer_service.notifications import heads_of_sales
        from django.contrib.auth import get_user_model

        User = get_user_model()
        emails = _contact_emails_for_contract(contract)

        recipients = set(heads_of_sales())
        for u in User.objects.filter(
            is_active=True, groups__name__in=['CEO', 'Admin', 'Office Manager'],
        ):
            recipients.add(u)

        cp_notifications.send_on_site_notice(
            event=event,
            contact_emails=emails,
            internal_recipients=recipients,
        )
    except Exception:
        logger.exception('tasks.on_site: fan-out failed')


def _notify_pending_verify(task, cs_owner, contact_emails):
    try:
        from apps.customer_portal import notifications as cp_notifications
        cp_notifications.send_task_done_pending_verify(
            task=task,
            cs_owner=cs_owner,
            contact_emails=contact_emails,
        )
    except Exception:
        logger.exception('tasks.mark_done: pending-verify notify failed')


def _resolve_cs_owner(task, assignment):
    """
    Who's the CS agent that should verify? In priority order:
      1. The assignment.assigned_by user (the CS agent who routed the
         ticket).
      2. The ticket's current_owner.
      3. None.
    """
    if not assignment:
        return None
    if assignment.assigned_by_id:
        return assignment.assigned_by
    try:
        ticket = assignment.ticket
        return getattr(ticket, 'current_owner', None)
    except Exception:
        return None
