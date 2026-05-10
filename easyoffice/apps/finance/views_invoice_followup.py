"""
apps/finance/views_invoice_followup.py
======================================

NEW MODULE — handles the "assign follow-up task" action on the
IncomingPaymentRequest detail page.

  * IncomingPaymentRequestAddFollowUpView
        POST /finance/invoices/<uuid:pk>/follow-up/
        name='incoming_payment_request_add_followup'

The created Task is linked back to the invoice via the
`Task.incoming_payment` FK added in apps.tasks.models, so:
  * the invoice detail page can list its follow-ups
  * the task detail page can show a "Following up on invoice X" link

If you'd rather keep all finance views in views.py, paste the body of
IncomingPaymentRequestAddFollowUpView there and skip this file. Either
way add the URL route shown in urls_invoice_followup_snippet.txt.
"""
from __future__ import annotations

import logging
from datetime import datetime, time

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect
from django.utils import timezone
from django.views.generic import View

from apps.finance.models import IncomingPaymentRequest
from apps.tasks.models import Task

logger = logging.getLogger(__name__)


# Permission gate. Loose by design: anyone in finance, the invoice
# creator, the CEO/Admin, or a superuser can assign a follow-up.
def _can_assign_followup(user, ipr) -> bool:
    if not user or not getattr(user, 'is_authenticated', False):
        return False
    if user.is_superuser:
        return True
    # Same approximate gate the rest of the finance app uses.
    try:
        groups = {g.lower() for g in user.groups.values_list('name', flat=True)}
    except Exception:
        groups = set()
    finance_or_owner = (
        bool(groups & {'finance', 'finance officer', 'cfo',
                       'ceo', 'admin', 'office manager', 'administrator'})
        or (ipr and ipr.created_by_id == user.pk)
    )
    return finance_or_owner


def _parse_due_date(raw):
    """
    Accepts 'YYYY-MM-DD' or 'YYYY-MM-DDTHH:MM' from a date / datetime-local
    input. Returns a timezone-aware datetime, or None if blank/invalid.
    """
    s = (raw or '').strip()
    if not s:
        return None
    for fmt in ('%Y-%m-%dT%H:%M', '%Y-%m-%d %H:%M', '%Y-%m-%d'):
        try:
            naive = datetime.strptime(s, fmt)
            break
        except ValueError:
            naive = None
    if naive is None:
        return None
    # Date-only inputs default to end-of-day so the task isn't "overdue
    # by 9 hours" the moment the day starts.
    if naive.time() == time(0, 0):
        naive = datetime.combine(naive.date(), time(17, 0))
    tz = timezone.get_current_timezone()
    try:
        return timezone.make_aware(naive, tz)
    except Exception:
        return naive  # fall back to naive — safer than crashing


class IncomingPaymentRequestAddFollowUpView(LoginRequiredMixin, View):
    """
    Create a follow-up Task linked back to the invoice.

    Form fields:
        assigned_to     — required, user pk
        due_date        — required, YYYY-MM-DD or datetime-local string
        priority        — optional, defaults to 'medium'
        title           — optional; if omitted we generate one from the invoice
        notes           — optional; appended to the task description
    """
    http_method_names = ['post']

    def post(self, request, pk):
        ipr = get_object_or_404(IncomingPaymentRequest, pk=pk)

        if not _can_assign_followup(request.user, ipr):
            return HttpResponseForbidden(
                'You do not have permission to assign follow-up tasks on this invoice.'
            )

        # ── Resolve assignee ───────────────────────────────────────────
        assigned_to_id = (request.POST.get('assigned_to') or '').strip()
        if not assigned_to_id:
            messages.error(request, 'Please pick someone to assign the follow-up to.')
            return redirect('incoming_payment_request_detail', pk=ipr.pk)

        from apps.core.models import User
        assigned_to = User.objects.filter(pk=assigned_to_id, is_active=True).first()
        if not assigned_to:
            messages.error(request, 'Selected user is not available.')
            return redirect('incoming_payment_request_detail', pk=ipr.pk)

        # ── Due date ───────────────────────────────────────────────────
        due_date = _parse_due_date(request.POST.get('due_date'))
        if due_date is None:
            messages.error(request, 'Please pick a valid due date.')
            return redirect('incoming_payment_request_detail', pk=ipr.pk)

        # ── Title + description ────────────────────────────────────────
        explicit_title = (request.POST.get('title') or '').strip()
        title = explicit_title or (
            f'Follow up on invoice {ipr.invoice_number} — {ipr.customer_name}'
        )[:300]

        notes = (request.POST.get('notes') or '').strip()
        # Build a description that's useful to the assignee even if they
        # never click through — they can see the customer, the amount,
        # and whether a reminder was already sent.
        amount_line = f'{ipr.currency} {ipr.total_amount}' if ipr.total_amount else ''
        contact_line = ipr.customer_email or ipr.customer_phone or ''
        last_reminder_line = (
            f'Last reminder: {ipr.last_reminder_sent.strftime("%b %d, %Y")}'
            if getattr(ipr, 'last_reminder_sent', None) else
            'No reminders sent yet.'
        )
        overdue_line = (
            f'⚠️ Overdue by {ipr.days_overdue} day(s).'
            if getattr(ipr, 'is_overdue', False) and getattr(ipr, 'days_overdue', 0) else
            ''
        )

        description_parts = [
            f'Follow up on payment for invoice {ipr.invoice_number}.',
            '',
            f'Customer:  {ipr.customer_name}'
            + (f' ({ipr.customer_company})' if ipr.customer_company else ''),
            f'Contact:   {contact_line}' if contact_line else '',
            f'Amount:    {amount_line}' if amount_line else '',
            f'Status:    {ipr.get_status_display()}',
            last_reminder_line,
            overdue_line,
        ]
        if notes:
            description_parts += ['', '── Notes from finance ──', notes]

        description = '\n'.join(line for line in description_parts if line is not None)

        # ── Priority ───────────────────────────────────────────────────
        priority = (request.POST.get('priority') or 'medium').strip().lower()
        if priority not in {'low', 'medium', 'high', 'urgent', 'critical'}:
            priority = 'medium'
        # Auto-bump if the invoice is overdue and the user didn't pick high.
        if getattr(ipr, 'is_overdue', False) and priority in {'low', 'medium'}:
            priority = 'high'

        # ── Create the task ────────────────────────────────────────────
        task = Task.objects.create(
            title=title,
            description=description,
            assigned_to=assigned_to,
            assigned_by=request.user,
            priority=priority,
            status='todo',
            due_date=due_date,
            incoming_payment=ipr,
        )

        # Best-effort notification — reuse existing tasks helper if present
        try:
            from apps.tasks.views import _notify_task_watchers
            _notify_task_watchers(
                task,
                request.user,
                'New follow-up assigned',
                (
                    f'{request.user.full_name} asked you to follow up on '
                    f'invoice {ipr.invoice_number} ({ipr.customer_name}). '
                    f'Due {due_date.strftime("%b %d, %Y")}.'
                ),
            )
        except Exception:
            logger.warning('invoice followup: notify failed', exc_info=True)

        messages.success(
            request,
            f'Follow-up assigned to {assigned_to.full_name}, due '
            f'{due_date.strftime("%b %d, %Y")}.',
        )
        return redirect('incoming_payment_request_detail', pk=ipr.pk)
