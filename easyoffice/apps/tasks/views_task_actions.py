"""
apps/tasks/views_task_actions.py
=================================

NEW MODULE — handles task-level actions that aren't field updates:

  * TaskDeleteView   — creator deletes a task that has no activity yet
  * TaskPinView      — pin a task (anyone with edit rights)
  * TaskUnpinView    — un-pin

URL wiring (added to apps/tasks/urls.py):
    path('<uuid:pk>/delete/',
         views_task_actions.TaskDeleteView.as_view(),
         name='task_delete'),
    path('<uuid:pk>/pin/',
         views_task_actions.TaskPinView.as_view(),
         name='task_pin'),
    path('<uuid:pk>/unpin/',
         views_task_actions.TaskUnpinView.as_view(),
         name='task_unpin'),
"""
from __future__ import annotations

import logging

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.views.generic import View

from apps.tasks.models import Task

logger = logging.getLogger(__name__)


def _can_full_edit_task(user, task):
    from apps.tasks.views import _can_full_edit_task as _impl
    return _impl(user, task)


def _is_admin_user(user):
    from apps.tasks.views import _is_admin_user as _impl
    return _impl(user)


# ─────────────────────────────────────────────────────────────────────────────
# Delete (only when there's no activity)
# ─────────────────────────────────────────────────────────────────────────────

class TaskDeleteView(LoginRequiredMixin, View):
    """
    POST /tasks/<pk>/delete/

    Rules:
      * The task creator (assigned_by) can delete the task IF and ONLY IF
        the task has no activity (`task.has_activity == False`).
      * Admins / CEO / Office Manager / superusers can delete at any time.

    `has_activity` is defined on the Task model — see models_patch.py.
    Activity = comments, time-logs, attachments, reassignments, on-site
    events, or status moved beyond TODO.
    """
    http_method_names = ['post']

    def post(self, request, pk):
        task = get_object_or_404(Task, pk=pk)
        is_creator = (task.assigned_by_id == request.user.id)
        is_admin = _is_admin_user(request.user)

        if not (is_creator or is_admin):
            return HttpResponseForbidden('Only the creator or an admin can delete this task.')

        # Non-admin creators are only allowed to delete clean tasks
        if is_creator and not is_admin and task.has_activity:
            messages.error(
                request,
                'This task has activity (comments, time-logs, attachments, '
                'reassignments, or progress) — it can no longer be deleted. '
                'Cancel it instead.',
            )
            return redirect('task_detail', pk=task.pk)

        title = task.title
        task.delete()
        messages.success(request, f'Task “{title}” was deleted.')
        return redirect('task_list')


# ─────────────────────────────────────────────────────────────────────────────
# Pin / unpin
# ─────────────────────────────────────────────────────────────────────────────

class TaskPinView(LoginRequiredMixin, View):
    """
    POST /tasks/<pk>/pin/

    Anyone with edit rights on the task can pin it. Pinned tasks float
    to the top of the task list for CEO / Admin / Office Manager and
    for supervisors viewing their team.
    """
    http_method_names = ['post']

    def post(self, request, pk):
        task = get_object_or_404(Task, pk=pk)
        if not _can_full_edit_task(request.user, task):
            return HttpResponseForbidden('You cannot pin this task.')

        moved = task.pin(by_user=request.user)

        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return JsonResponse({'status': 'ok', 'is_pinned': True, 'changed': moved})

        if moved:
            messages.success(request, '📌 Task pinned to the top.')
        else:
            messages.info(request, 'Task is already pinned.')
        return redirect('task_detail', pk=task.pk)


class TaskUnpinView(LoginRequiredMixin, View):
    http_method_names = ['post']

    def post(self, request, pk):
        task = get_object_or_404(Task, pk=pk)
        if not _can_full_edit_task(request.user, task):
            return HttpResponseForbidden('You cannot unpin this task.')

        moved = task.unpin()

        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return JsonResponse({'status': 'ok', 'is_pinned': False, 'changed': moved})

        if moved:
            messages.success(request, 'Task unpinned.')
        else:
            messages.info(request, 'Task was not pinned.')
        return redirect('task_detail', pk=task.pk)
