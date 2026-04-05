from django.contrib.auth.mixins import LoginRequiredMixin
from django.views.generic import TemplateView
from django.db.models import Count, Q
from django.utils import timezone

from apps.core.models import User
from apps.tasks.models import Task
from apps.projects.models import Project
from apps.staff.models import LeaveRequest


class DashboardView(LoginRequiredMixin, TemplateView):
    """
    Role-aware dashboard:
    - Admin / CEO / Office Manager / superuser -> office-wide dashboard
    - Supervisor -> own dashboard + team widgets
    - Staff -> personal dashboard
    """

    def _is_admin_user(self, user):
        return user.is_superuser or user.groups.filter(
            name__in=['CEO', 'Admin', 'Office Manager']
        ).exists()

    def _get_staff_profile(self, user):
        return getattr(user, 'staffprofile', None)

    def get_template_names(self):
        user = self.request.user

        if self._is_admin_user(user):
            return ['dashboard/admin_dashboard.html']

        profile = self._get_staff_profile(user)
        if profile and profile.is_supervisor_role:
            return ['dashboard/supervisor_dashboard.html']

        return ['dashboard/staff_dashboard.html']

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        user = self.request.user
        now = timezone.now()
        today = now.date()

        ctx['now'] = now
        ctx['is_admin_user'] = self._is_admin_user(user)

        profile = self._get_staff_profile(user)
        ctx['staff_profile'] = profile
        ctx['is_supervisor_user'] = bool(profile and profile.is_supervisor_role)

        # ------------------------------------------------------------------
        # Personal task summary
        # ------------------------------------------------------------------
        my_tasks = user.assigned_tasks.all()
        ctx['my_tasks_total'] = my_tasks.count()
        ctx['my_tasks_todo'] = my_tasks.filter(status='todo').count()
        ctx['my_tasks_in_progress'] = my_tasks.filter(status='in_progress').count()
        ctx['my_tasks_review'] = my_tasks.filter(status='review').count()
        ctx['my_tasks_done'] = my_tasks.filter(status='done').count()
        ctx['my_tasks_overdue'] = my_tasks.filter(
            status__in=['todo', 'in_progress', 'review'],
            due_date__lt=now
        ).count()
        ctx['my_tasks_due_today'] = my_tasks.filter(
            status__in=['todo', 'in_progress', 'review'],
            due_date__date=today
        ).count()

        ctx['recent_tasks'] = my_tasks.filter(
            status__in=['todo', 'in_progress', 'review']
        ).select_related(
            'assigned_by', 'project', 'category'
        ).order_by(
            'due_date', '-created_at'
        )[:6]

        # ------------------------------------------------------------------
        # Personal projects
        # ------------------------------------------------------------------
        my_projects = user.projects.filter(status='active').select_related(
            'project_manager'
        ).order_by('-updated_at')

        ctx['my_projects'] = my_projects[:4]
        ctx['my_projects_count'] = my_projects.count()

        # ------------------------------------------------------------------
        # Notifications
        # ------------------------------------------------------------------
        ctx['recent_notifications'] = user.core_notifications.filter(
            is_read=False
        ).order_by('-created_at')[:5]

        # ------------------------------------------------------------------
        # Personal leave snapshot
        # ------------------------------------------------------------------
        ctx['my_pending_leave_requests'] = user.leave_requests.filter(status='pending').count()
        ctx['my_approved_leave_requests'] = user.leave_requests.filter(status='approved').count()
        ctx['my_recent_leave_requests'] = user.leave_requests.select_related(
            'leave_type', 'approved_by'
        ).order_by('-created_at')[:5]

        # ------------------------------------------------------------------
        # Supervisor widgets
        # user.supervisees returns StaffProfile objects, not User objects
        # ------------------------------------------------------------------
        if profile and profile.is_supervisor_role:
            supervisee_profiles = user.supervisees.select_related(
                'user', 'unit', 'department', 'position'
            ).filter(
                user__is_active=True,
                user__status='active'
            )

            supervisee_user_ids = [sp.user_id for sp in supervisee_profiles]

            ctx['team_count'] = supervisee_profiles.count()

            team_tasks = Task.objects.filter(
                assigned_to_id__in=supervisee_user_ids
            )

            ctx['team_tasks_total'] = team_tasks.count()
            ctx['team_tasks_overdue'] = team_tasks.filter(
                status__in=['todo', 'in_progress', 'review'],
                due_date__lt=now
            ).count()
            ctx['team_tasks_due_today'] = team_tasks.filter(
                status__in=['todo', 'in_progress', 'review'],
                due_date__date=today
            ).count()

            ctx['team_recent_tasks'] = team_tasks.select_related(
                'assigned_to', 'assigned_by', 'project'
            ).order_by(
                'due_date', '-updated_at'
            )[:6]

            ctx['team_recent_leave_requests'] = LeaveRequest.objects.filter(
                staff_id__in=supervisee_user_ids
            ).select_related(
                'staff', 'leave_type', 'approved_by'
            ).order_by('-created_at')[:6]

            ctx['team_pending_leave_requests'] = LeaveRequest.objects.filter(
                staff_id__in=supervisee_user_ids,
                status='pending'
            ).count()

            ctx['team_members'] = [sp.user for sp in supervisee_profiles[:8]]

        # ------------------------------------------------------------------
        # Admin widgets
        # ------------------------------------------------------------------
        if self._is_admin_user(user):
            total_staff_qs = User.objects.filter(is_active=True, status='active')
            total_projects_qs = Project.objects.all()
            total_open_tasks_qs = Task.objects.filter(status__in=['todo', 'in_progress', 'review'])

            ctx['total_staff'] = total_staff_qs.count()
            ctx['total_active_projects'] = total_projects_qs.filter(status='active').count()
            ctx['total_tasks_open'] = total_open_tasks_qs.count()
            ctx['total_tasks_overdue'] = total_open_tasks_qs.filter(due_date__lt=now).count()

            ctx['projects_by_status'] = total_projects_qs.values('status').annotate(
                count=Count('id')
            ).order_by('status')

            ctx['latest_projects'] = total_projects_qs.select_related(
                'project_manager'
            ).order_by('-created_at')[:5]

            ctx['office_pending_leave_requests'] = LeaveRequest.objects.filter(
                status='pending'
            ).count()

            ctx['office_recent_leave_requests'] = LeaveRequest.objects.select_related(
                'staff', 'leave_type', 'approved_by'
            ).order_by('-created_at')[:8]

        return ctx


class AdminDashboardView(LoginRequiredMixin, TemplateView):
    template_name = 'dashboard/admin_dashboard.html'

    def dispatch(self, request, *args, **kwargs):
        if not (
            request.user.is_superuser or
            request.user.groups.filter(name__in=['CEO', 'Admin', 'Office Manager']).exists()
        ):
            from django.http import HttpResponseForbidden
            return HttpResponseForbidden('You do not have permission to access this dashboard.')
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        # Keep this route usable, but main dashboard already routes admins here.
        ctx = super().get_context_data(**kwargs)
        now = timezone.now()

        total_staff_qs = User.objects.filter(is_active=True, status='active')
        total_projects_qs = Project.objects.all()
        total_open_tasks_qs = Task.objects.filter(status__in=['todo', 'in_progress', 'review'])

        ctx['now'] = now
        ctx['total_staff'] = total_staff_qs.count()
        ctx['total_active_projects'] = total_projects_qs.filter(status='active').count()
        ctx['total_tasks_open'] = total_open_tasks_qs.count()
        ctx['total_tasks_overdue'] = total_open_tasks_qs.filter(due_date__lt=now).count()
        ctx['projects_by_status'] = total_projects_qs.values('status').annotate(count=Count('id')).order_by('status')
        ctx['latest_projects'] = total_projects_qs.select_related('project_manager').order_by('-created_at')[:5]

        return ctx