from django.contrib.auth.mixins import LoginRequiredMixin
from django.views.generic import TemplateView, View, DetailView
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.http import JsonResponse, HttpResponseForbidden
from django.utils import timezone
from django.db.models import Q, Sum

from apps.tasks.models import Task, TaskComment, TaskAttachment, TaskReassignment, TaskTimeLog
from apps.core.models import User, CoreNotification


def _get_staffprofile(user):
    return getattr(user, 'staffprofile', None)


def _is_admin_user(user):
    """CEO, Admin, Office Manager, and superusers can see all tasks."""
    return user.is_superuser or user.groups.filter(
        name__in=['CEO', 'Admin', 'Office Manager']
    ).exists()


def _is_supervisor_of(user, target_user):
    target_profile = getattr(target_user, 'staffprofile', None)
    return bool(target_profile and target_profile.supervisor_id == user.id)


def _can_view_task(user, task):
    if _is_admin_user(user):
        return True
    if task.assigned_by_id == user.id:
        return True
    if task.assigned_to_id == user.id:
        return True
    if task.collaborators.filter(id=user.id).exists():
        return True
    if _is_supervisor_of(user, task.assigned_to):
        return True
    return False


def _can_full_edit_task(user, task):
    """
    Only creator/assigner, admins, and superuser can fully edit task fields.
    """
    if _is_admin_user(user):
        return True
    if task.assigned_by_id == user.id:
        return True
    return False


def _can_update_task_progress(user, task):
    """
    Quick update permission:
    - admins / CEO / Office Manager
    - creator/assigner
    - assignee
    - supervisor of assignee
    """
    if _is_admin_user(user):
        return True
    if task.assigned_by_id == user.id:
        return True
    if task.assigned_to_id == user.id:
        return True
    if _is_supervisor_of(user, task.assigned_to):
        return True
    return False


def _can_reassign_task(user, task):
    """
    Reassignment:
    - admins / CEO / Office Manager
    - creator/assigner
    - supervisor of assignee
    """
    if _is_admin_user(user):
        return True
    if task.assigned_by_id == user.id:
        return True
    if _is_supervisor_of(user, task.assigned_to):
        return True
    return False


def _can_log_time(user, task):
    if _is_admin_user(user):
        return True
    if task.assigned_to_id == user.id:
        return True
    if task.collaborators.filter(id=user.id).exists():
        return True
    if _is_supervisor_of(user, task.assigned_to):
        return True
    return False


def _notify_task_watchers(task, sender, title, message):
    recipients = set()

    if task.assigned_to and task.assigned_to != sender:
        recipients.add(task.assigned_to)

    if task.assigned_by and task.assigned_by != sender:
        recipients.add(task.assigned_by)

    assignee_profile = getattr(task.assigned_to, 'staffprofile', None)
    if assignee_profile and assignee_profile.supervisor and assignee_profile.supervisor != sender:
        recipients.add(assignee_profile.supervisor)

    for collaborator in task.collaborators.exclude(id=sender.id):
        recipients.add(collaborator)

    for recipient in recipients:
        CoreNotification.objects.create(
            recipient=recipient,
            sender=sender,
            notification_type='task',
            title=title,
            message=message,
            link=f'/tasks/{task.id}/',
        )


class TaskListView(LoginRequiredMixin, TemplateView):
    template_name = 'tasks/task_list.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        user = self.request.user
        q = self.request.GET.get('q', '')
        status = self.request.GET.get('status', '')
        priority = self.request.GET.get('priority', '')
        view_mode = self.request.GET.get('view', 'mine')

        staff_profile = _get_staffprofile(user)
        is_admin = _is_admin_user(user)

        if view_mode == 'all' and is_admin:
            # CEO / Admin / Office Manager / superuser — see every task in org
            tasks = Task.objects.all()
        elif view_mode == 'team' and staff_profile and staff_profile.is_supervisor_role:
            tasks = Task.objects.filter(
                assigned_to__staffprofile__supervisor=user
            )
        else:
            tasks = Task.objects.filter(
                Q(assigned_to=user) |
                Q(collaborators=user) |
                Q(assigned_by=user)
            ).distinct()

        if q:
            tasks = tasks.filter(Q(title__icontains=q) | Q(description__icontains=q))
        if status:
            tasks = tasks.filter(status=status)
        if priority:
            tasks = tasks.filter(priority=priority)

        ctx['tasks'] = tasks.select_related(
            'assigned_to', 'assigned_by', 'category', 'project'
        ).order_by('-created_at')
        ctx['status_choices'] = Task.Status.choices
        ctx['priority_choices'] = Task.Priority.choices
        ctx['view_mode'] = view_mode
        ctx['q'] = q
        ctx['status_filter'] = status
        ctx['priority_filter'] = priority
        ctx['task_counts'] = {
            'todo': tasks.filter(status='todo').count(),
            'in_progress': tasks.filter(status='in_progress').count(),
            'review': tasks.filter(status='review').count(),
            'done': tasks.filter(status='done').count(),
        }
        ctx['can_view_team_tasks'] = bool(staff_profile and staff_profile.is_supervisor_role)
        ctx['can_view_all_tasks']  = is_admin
        return ctx


class TaskDetailView(LoginRequiredMixin, DetailView):
    model = Task
    template_name = 'tasks/task_detail.html'
    context_object_name = 'task'

    def dispatch(self, request, *args, **kwargs):
        task = self.get_object()
        if not _can_view_task(request.user, task):
            return HttpResponseForbidden('You do not have permission to view this task.')
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        task = self.object

        ctx['comments'] = task.comments.select_related('author').all()
        ctx['attachments'] = task.attachments.select_related('uploaded_by').all()
        ctx['time_logs'] = task.time_logs.select_related('user').all()
        ctx['subtasks'] = task.subtasks.all()
        ctx['reassignments'] = task.reassignments.select_related(
            'from_user', 'to_user', 'reassigned_by'
        ).all()
        ctx['staff_list'] = User.objects.filter(
            status='active',
            is_active=True
        ).exclude(id=task.assigned_to_id)

        ctx['can_full_edit_task'] = _can_full_edit_task(self.request.user, task)
        ctx['can_update_task_progress'] = _can_update_task_progress(self.request.user, task)
        ctx['can_reassign_task'] = _can_reassign_task(self.request.user, task)
        ctx['can_log_time'] = _can_log_time(self.request.user, task)
        ctx['can_comment'] = _can_view_task(self.request.user, task)
        ctx['is_supervisor_monitor'] = _is_supervisor_of(self.request.user, task.assigned_to)
        return ctx


class TaskCreateView(LoginRequiredMixin, View):
    template_name = 'tasks/task_form.html'

    def get(self, request):
        from apps.tasks.models import TaskCategory
        from apps.projects.models import Project
        ctx = {
            'staff_list': User.objects.filter(status='active', is_active=True),
            'categories': TaskCategory.objects.all(),
            'projects': Project.objects.filter(status='active'),
            'priority_choices': Task.Priority.choices,
            'mode': 'create',
        }
        return render(request, self.template_name, ctx)

    def post(self, request):
        assigned_to_id = request.POST.get('assigned_to')
        assigned_to = get_object_or_404(User, id=assigned_to_id)

        task = Task.objects.create(
            title=request.POST['title'],
            description=request.POST.get('description', ''),
            assigned_to=assigned_to,
            assigned_by=request.user,
            priority=request.POST.get('priority', 'medium'),
            status='todo',
            due_date=request.POST.get('due_date') or None,
            estimated_hours=request.POST.get('estimated_hours') or None,
            category_id=request.POST.get('category') or None,
            project_id=request.POST.get('project') or None,
        )

        collaborator_ids = request.POST.getlist('collaborators')
        if collaborator_ids:
            task.collaborators.set(User.objects.filter(id__in=collaborator_ids))

        _notify_task_watchers(
            task,
            request.user,
            'New Task Assigned',
            f'{request.user.full_name} assigned task "{task.title}".'
        )

        messages.success(request, f'Task "{task.title}" created and assigned.')
        return redirect('task_detail', pk=task.id)


class TaskUpdateView(LoginRequiredMixin, View):
    template_name = 'tasks/task_form.html'

    def get(self, request, pk):
        task = get_object_or_404(Task, pk=pk)
        if not _can_full_edit_task(request.user, task):
            return HttpResponseForbidden('You do not have permission to fully edit this task.')

        from apps.tasks.models import TaskCategory
        from apps.projects.models import Project

        ctx = {
            'task': task,
            'staff_list': User.objects.filter(status='active', is_active=True),
            'categories': TaskCategory.objects.all(),
            'projects': Project.objects.filter(status='active'),
            'priority_choices': Task.Priority.choices,
            'status_choices': Task.Status.choices,
            'mode': 'edit',
            'can_reassign_task': _can_reassign_task(request.user, task),
            'is_supervisor_monitor': _is_supervisor_of(request.user, task.assigned_to),
        }
        return render(request, self.template_name, ctx)

    def post(self, request, pk):
        task = get_object_or_404(Task, pk=pk)
        if not _can_full_edit_task(request.user, task):
            return HttpResponseForbidden('You do not have permission to fully edit this task.')

        old_status = task.status
        old_assigned_to_id = task.assigned_to_id

        task.title = request.POST.get('title', task.title)
        task.description = request.POST.get('description', task.description)
        task.priority = request.POST.get('priority', task.priority)
        task.status = request.POST.get('status', task.status)
        task.due_date = request.POST.get('due_date') or None
        task.estimated_hours = request.POST.get('estimated_hours') or None
        task.progress_pct = int(request.POST.get('progress_pct', task.progress_pct or 0))
        task.category_id = request.POST.get('category') or None
        task.project_id = request.POST.get('project') or None

        assigned_to_id = request.POST.get('assigned_to')
        if assigned_to_id:
            task.assigned_to = get_object_or_404(User, id=assigned_to_id)

        task.save()

        collaborator_ids = request.POST.getlist('collaborators')
        task.collaborators.set(User.objects.filter(id__in=collaborator_ids))

        if task.status == 'done' and old_status != 'done':
            task.completed_at = timezone.now()
            task.progress_pct = 100
            task.save(update_fields=['completed_at', 'progress_pct'])
        elif task.status != 'done' and old_status == 'done':
            task.completed_at = None
            task.save(update_fields=['completed_at'])

        if old_assigned_to_id != task.assigned_to_id:
            TaskReassignment.objects.create(
                task=task,
                from_user_id=old_assigned_to_id,
                to_user=task.assigned_to,
                reassigned_by=request.user,
                is_full_reassignment=True,
                notes='Updated from task edit form.'
            )

        messages.success(request, 'Task updated successfully.')
        return redirect('task_detail', pk=task.id)


class TaskQuickUpdateView(LoginRequiredMixin, View):
    """
    Quick update for status/progress.
    If a user marks a task as done or sets progress to 100%,
    they must explain what was completed.
    """

    def _error(self, request, task, message, status=400):
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return JsonResponse(
                {'status': 'error', 'message': message},
                status=status
            )
        messages.error(request, message)
        return redirect('task_detail', pk=task.pk)

    def post(self, request, pk):
        task = get_object_or_404(Task, pk=pk)

        if not _can_update_task_progress(request.user, task):
            return self._error(
                request,
                task,
                'You do not have permission to update this task.',
                status=403
            )

        status_value = request.POST.get('status')
        progress_value = request.POST.get('progress_pct')
        completion_comment = request.POST.get('completion_comment', '').strip()

        changed_fields = []
        target_status = task.status
        target_progress = task.progress_pct or 0

        if status_value in dict(Task.Status.choices):
            target_status = status_value

        if progress_value not in [None, '']:
            try:
                target_progress = max(0, min(100, int(progress_value)))
            except ValueError:
                return self._error(request, task, 'Invalid progress value.')

        will_complete = target_status == 'done' or target_progress == 100

        if will_complete and not completion_comment:
            return self._error(
                request,
                task,
                'Please add a completion comment explaining what was done before marking this task as completed.'
            )

        if status_value in dict(Task.Status.choices):
            task.status = status_value
            changed_fields.append('status')

            if status_value == 'done':
                task.completed_at = timezone.now()
                task.progress_pct = 100
                target_progress = 100
                if 'progress_pct' not in changed_fields:
                    changed_fields.append('progress_pct')
                if 'completed_at' not in changed_fields:
                    changed_fields.append('completed_at')
            elif task.completed_at:
                task.completed_at = None
                if 'completed_at' not in changed_fields:
                    changed_fields.append('completed_at')

        if progress_value not in [None, '']:
            task.progress_pct = target_progress
            if 'progress_pct' not in changed_fields:
                changed_fields.append('progress_pct')

            if target_progress == 100 and task.status != 'done':
                task.status = 'done'
                if 'status' not in changed_fields:
                    changed_fields.append('status')
                task.completed_at = timezone.now()
                if 'completed_at' not in changed_fields:
                    changed_fields.append('completed_at')

        if not changed_fields:
            return self._error(request, task, 'Nothing to update.')

        task.save(update_fields=changed_fields)

        if will_complete:
            TaskComment.objects.create(
                task=task,
                author=request.user,
                content=f'✅ Completion note:\n{completion_comment}'
            )

        _notify_task_watchers(
            task,
            request.user,
            'Task Updated',
            f'{request.user.full_name} updated "{task.title}" to {task.get_status_display()} ({task.progress_pct}%).'
        )

        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return JsonResponse({
                'status': 'ok',
                'new_status': task.get_status_display(),
                'progress_pct': task.progress_pct,
            })

        messages.success(request, 'Task updated successfully.')
        return redirect('task_detail', pk=task.pk)


class TaskCommentView(LoginRequiredMixin, View):
    def post(self, request, pk):
        task = get_object_or_404(Task, pk=pk)
        if not _can_view_task(request.user, task):
            return HttpResponseForbidden('You do not have permission to comment on this task.')

        content = request.POST.get('content', '').strip()
        if content:
            TaskComment.objects.create(task=task, author=request.user, content=content)
            try:
                from apps.projects.views import sync_milestone_from_task
                sync_milestone_from_task(task)
            except Exception:
                # Never block a task save on a project-side hiccup; log only.
                import logging
                logging.getLogger(__name__).exception(
                    'Failed to sync milestone from task %s', task.pk
                )

            _notify_task_watchers(
                task,
                request.user,
                'Task Updated',
                f'{request.user.full_name} updated "{task.title}" to {task.get_status_display()} ({task.progress_pct}%).'
            )
        return redirect('task_detail', pk=pk)


class TaskReassignView(LoginRequiredMixin, View):
    def post(self, request, pk):
        task = get_object_or_404(Task, pk=pk)
        if not _can_reassign_task(request.user, task):
            return HttpResponseForbidden('You do not have permission to reassign this task.')

        to_user_id = request.POST.get('to_user')
        to_user = get_object_or_404(User, id=to_user_id)
        is_full = request.POST.get('is_full', 'true') == 'true'
        notes = request.POST.get('notes', '')

        TaskReassignment.objects.create(
            task=task,
            from_user=task.assigned_to,
            to_user=to_user,
            reassigned_by=request.user,
            is_full_reassignment=is_full,
            portion_description=request.POST.get('portion_description', ''),
            notes=notes,
        )

        if is_full:
            task.assigned_to = to_user
            task.save(update_fields=['assigned_to'])
        else:
            task.collaborators.add(to_user)

        _notify_task_watchers(
            task,
            request.user,
            'Task Assignment Updated',
            f'Task "{task.title}" has been {"fully reassigned" if is_full else "partially delegated"} to {to_user.full_name}.'
        )

        messages.success(
            request,
            f'Task {"reassigned" if is_full else "partially delegated"} to {to_user.full_name}.'
        )
        return redirect('task_detail', pk=pk)


class TaskTimeLogView(LoginRequiredMixin, View):
    def post(self, request, pk):
        task = get_object_or_404(Task, pk=pk)
        if not _can_log_time(request.user, task):
            return HttpResponseForbidden('You do not have permission to log time on this task.')

        hours = request.POST.get('hours')
        date = request.POST.get('date')
        if hours and date:
            TaskTimeLog.objects.create(
                task=task,
                user=request.user,
                hours=hours,
                date=date,
                description=request.POST.get('description', '')
            )

            total = task.time_logs.aggregate(t=Sum('hours'))['t'] or 0
            task.actual_hours = total
            task.save(update_fields=['actual_hours'])

            _notify_task_watchers(
                task,
                request.user,
                'Time Logged on Task',
                f'{request.user.full_name} logged {hours}h on "{task.title}".'
            )

        return redirect('task_detail', pk=pk)