from django.contrib.auth.mixins import LoginRequiredMixin
from django.views.generic import TemplateView, DetailView, View
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.db.models import Q, Count
from django.utils import timezone
from apps.projects.models import Project, Milestone, ProjectUpdate, Risk
from apps.core.models import User


def _is_project_member(user, project):
    return (
        user.is_superuser or
        project.project_manager == user or
        project.team_members.filter(id=user.id).exists()
    )


# ---------------------------------------------------------------------------
# Project List
# ---------------------------------------------------------------------------

class ProjectListView(LoginRequiredMixin, TemplateView):
    template_name = 'projects/project_list.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        user = self.request.user
        view = self.request.GET.get('view', 'my')
        q = self.request.GET.get('q', '')
        status = self.request.GET.get('status', '')
        priority = self.request.GET.get('priority', '')

        if user.is_superuser or view == 'all':
            projects = Project.objects.filter(
                Q(is_public=True) | Q(team_members=user) | Q(project_manager=user)
            ).distinct()
        else:
            projects = Project.objects.filter(
                Q(team_members=user) | Q(project_manager=user)
            ).distinct()

        if q:
            projects = projects.filter(
                Q(name__icontains=q) | Q(code__icontains=q) | Q(description__icontains=q)
            )
        if status:
            projects = projects.filter(status=status)
        if priority:
            projects = projects.filter(priority=priority)

        projects = projects.select_related('project_manager', 'department').order_by('-updated_at')

        my_projects = Project.objects.filter(
            Q(team_members=user) | Q(project_manager=user)
        ).distinct()

        ctx.update({
            'projects': projects,
            'status_choices': Project.Status.choices,
            'priority_choices': Project.Priority.choices,
            'view': view,
            'q': q,
            'status_filter': status,
            'priority_filter': priority,
            'total_count': projects.count(),
            'active_count': my_projects.filter(status='active').count(),
            'completed_count': my_projects.filter(status='completed').count(),
            'my_count': my_projects.count(),
        })
        return ctx


# ---------------------------------------------------------------------------
# Project Detail
# ---------------------------------------------------------------------------

class ProjectDetailView(LoginRequiredMixin, DetailView):
    model = Project
    template_name = 'projects/project_detail.html'
    context_object_name = 'project'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        project = self.object
        user = self.request.user

        milestones = project.milestones.all().order_by('order', 'due_date')
        tasks = project.tasks.select_related('assigned_to').order_by('status', '-created_at')[:15]
        updates = project.updates.select_related('author').order_by('-created_at')[:15]
        risks = project.risks.select_related('owner')
        team = project.team_members.filter(is_active=True).select_related(
            'staffprofile', 'staffprofile__position'
        )

        total_ms = milestones.count()
        completed_ms = milestones.filter(status='completed').count()
        overdue_ms = milestones.filter(
            status__in=['pending', 'in_progress'],
            due_date__lt=timezone.now().date()
        ).count()

        budget_pct = 0
        if project.budget and project.budget > 0:
            budget_pct = min(100, int((project.budget_spent / project.budget) * 100))

        days_remaining = None
        days_remaining_abs = None
        is_overdue = False
        if project.end_date:
            delta = project.end_date - timezone.now().date()
            days_remaining = delta.days
            is_overdue = days_remaining < 0 and project.status not in ('completed', 'cancelled')
            days_remaining_abs = abs(days_remaining)

        project_tags = []
        if project.tags:
            project_tags = [tag.strip() for tag in project.tags.split(",") if tag.strip()]

        ctx.update({
            'milestones': milestones,
            'updates': updates,
            'risks': risks,
            'active_risks': risks.filter(is_resolved=False),
            'resolved_risks': risks.filter(is_resolved=True),
            'tasks': tasks,
            'team_members': team,
            'is_member': _is_project_member(user, project),
            'is_manager': user.is_superuser or project.project_manager == user,

            'total_ms': total_ms,
            'completed_ms': completed_ms,
            'overdue_ms': overdue_ms,
            'budget_pct': budget_pct,
            'days_remaining': days_remaining,
            'days_remaining_abs': days_remaining_abs,
            'is_overdue': is_overdue,
            'project_tags': project_tags,

            'status_choices': Project.Status.choices,
            'risk_levels': Risk.Level.choices,
            'milestone_statuses': Milestone.Status.choices,
            'staff_list': User.objects.filter(status='active', is_active=True).order_by('first_name'),
        })
        return ctx


# ---------------------------------------------------------------------------
# Project Create
# ---------------------------------------------------------------------------

class ProjectCreateView(LoginRequiredMixin, View):
    template_name = 'projects/project_form.html'

    def get(self, request):
        from apps.organization.models import Department, Unit
        return render(request, self.template_name, {
            'departments': Department.objects.filter(is_active=True),
            'units': Unit.objects.filter(is_active=True),
            'staff_list': User.objects.filter(status='active', is_active=True).order_by('first_name'),
            'priority_choices': Project.Priority.choices,
            'status_choices': Project.Status.choices,
            'mode': 'create',
        })

    def post(self, request):
        try:
            import shortuuid
            code = request.POST.get('code', '').strip() or shortuuid.ShortUUID().random(length=6).upper()
        except ImportError:
            import random, string
            code = request.POST.get('code', '').strip() or ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))

        project = Project.objects.create(
            name=request.POST['name'],
            code=code,
            description=request.POST.get('description', ''),
            project_manager_id=request.POST.get('project_manager') or request.user.id,
            priority=request.POST.get('priority', 'medium'),
            status=request.POST.get('status', 'planning'),
            start_date=request.POST.get('start_date') or None,
            end_date=request.POST.get('end_date') or None,
            budget=request.POST.get('budget') or None,
            cover_color=request.POST.get('cover_color', '#1e3a5f'),
            tags=request.POST.get('tags', ''),
            is_public=request.POST.get('is_public') == 'on',
        )
        member_ids = request.POST.getlist('team_members')
        if member_ids:
            project.team_members.set(User.objects.filter(id__in=member_ids))
        if request.POST.get('dept_id'):
            try:
                from apps.organization.models import Department
                project.department = Department.objects.get(id=request.POST['dept_id'])
                project.save(update_fields=['department'])
            except Exception:
                pass

        messages.success(request, f'Project "{project.name}" created.')
        return redirect('project_detail', pk=project.id)


# ---------------------------------------------------------------------------
# Project Edit
# ---------------------------------------------------------------------------

class ProjectEditView(LoginRequiredMixin, View):
    template_name = 'projects/project_form.html'

    def get(self, request, pk):
        project = get_object_or_404(Project, pk=pk)
        if not _is_project_member(request.user, project):
            messages.error(request, 'You do not have permission to edit this project.')
            return redirect('project_detail', pk=pk)
        from apps.organization.models import Department, Unit
        return render(request, self.template_name, {
            'project': project,
            'departments': Department.objects.filter(is_active=True),
            'units': Unit.objects.filter(is_active=True),
            'staff_list': User.objects.filter(status='active', is_active=True).order_by('first_name'),
            'priority_choices': Project.Priority.choices,
            'status_choices': Project.Status.choices,
            'mode': 'edit',
        })

    def post(self, request, pk):
        project = get_object_or_404(Project, pk=pk)
        if not _is_project_member(request.user, project):
            messages.error(request, 'Permission denied.')
            return redirect('project_detail', pk=pk)

        project.name = request.POST.get('name', project.name)
        project.description = request.POST.get('description', project.description)
        project.priority = request.POST.get('priority', project.priority)
        project.status = request.POST.get('status', project.status)
        project.start_date = request.POST.get('start_date') or None
        project.end_date = request.POST.get('end_date') or None
        project.budget = request.POST.get('budget') or None
        project.cover_color = request.POST.get('cover_color', project.cover_color)
        project.tags = request.POST.get('tags', project.tags)
        project.is_public = request.POST.get('is_public') == 'on'
        pm_id = request.POST.get('project_manager')
        if pm_id:
            try:
                project.project_manager = User.objects.get(id=pm_id)
            except User.DoesNotExist:
                pass
        if request.POST.get('dept_id'):
            try:
                from apps.organization.models import Department
                project.department = Department.objects.get(id=request.POST['dept_id'])
            except Exception:
                pass
        project.save()

        member_ids = request.POST.getlist('team_members')
        if member_ids is not None:
            project.team_members.set(User.objects.filter(id__in=member_ids))

        messages.success(request, f'Project "{project.name}" updated.')
        return redirect('project_detail', pk=pk)


# ---------------------------------------------------------------------------
# Post Update / Change Status + Progress
# ---------------------------------------------------------------------------

class ProjectUpdateStatusView(LoginRequiredMixin, View):
    def post(self, request, pk):
        project = get_object_or_404(Project, pk=pk)
        if not _is_project_member(request.user, project):
            messages.error(request, 'Permission denied.')
            return redirect('project_detail', pk=pk)

        content = request.POST.get('content', '').strip()
        new_status = request.POST.get('status', project.status)
        try:
            progress = max(0, min(100, int(request.POST.get('progress_pct', project.progress_pct))))
        except (ValueError, TypeError):
            progress = project.progress_pct

        if content:
            ProjectUpdate.objects.create(
                project=project,
                author=request.user,
                title=request.POST.get('title', ''),
                content=content,
                progress_at_time=progress,
                status_at_time=new_status,
            )

        project.status = new_status
        project.progress_pct = progress
        if new_status == 'completed' and not project.completed_at:
            project.completed_at = timezone.now()
        project.save(update_fields=['status', 'progress_pct', 'completed_at', 'updated_at'])
        messages.success(request, 'Project updated.')
        return redirect('project_detail', pk=pk)


# ---------------------------------------------------------------------------
# Add Milestone
# ---------------------------------------------------------------------------

class ProjectMilestoneView(LoginRequiredMixin, View):
    def post(self, request, pk):
        project = get_object_or_404(Project, pk=pk)
        if not _is_project_member(request.user, project):
            messages.error(request, 'Permission denied.')
            return redirect('project_detail', pk=pk)

        Milestone.objects.create(
            project=project,
            name=request.POST['name'],
            description=request.POST.get('description', ''),
            due_date=request.POST['due_date'],
            order=project.milestones.count() + 1,
        )
        messages.success(request, 'Milestone added.')
        return redirect('project_detail', pk=pk)


# ---------------------------------------------------------------------------
# Update Milestone Status
# ---------------------------------------------------------------------------

class MilestoneStatusView(LoginRequiredMixin, View):
    def post(self, request, pk, mid):
        project = get_object_or_404(Project, pk=pk)
        milestone = get_object_or_404(Milestone, pk=mid, project=project)
        if not _is_project_member(request.user, project):
            messages.error(request, 'Permission denied.')
            return redirect('project_detail', pk=pk)

        new_status = request.POST.get('status', milestone.status)
        milestone.status = new_status
        if new_status == 'completed' and not milestone.completed_at:
            milestone.completed_at = timezone.now()
        milestone.save()
        messages.success(request, f'Milestone "{milestone.name}" updated.')
        return redirect('project_detail', pk=pk)


# ---------------------------------------------------------------------------
# Risks
# ---------------------------------------------------------------------------

class ProjectRiskView(LoginRequiredMixin, View):
    def post(self, request, pk):
        project = get_object_or_404(Project, pk=pk)
        if not _is_project_member(request.user, project):
            messages.error(request, 'Permission denied.')
            return redirect('project_detail', pk=pk)

        action = request.POST.get('action', 'add')

        if action == 'resolve':
            risk = get_object_or_404(Risk, pk=request.POST.get('risk_id'), project=project)
            risk.is_resolved = True
            risk.save(update_fields=['is_resolved'])
            messages.success(request, f'Risk "{risk.title}" marked as resolved.')

        elif action == 'add':
            owner_id = request.POST.get('owner_id')
            Risk.objects.create(
                project=project,
                title=request.POST['title'],
                description=request.POST.get('description', ''),
                level=request.POST.get('level', 'medium'),
                mitigation=request.POST.get('mitigation', ''),
                owner_id=owner_id or None,
            )
            messages.success(request, 'Risk logged.')

        return redirect('project_detail', pk=pk)