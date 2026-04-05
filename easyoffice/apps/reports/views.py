from django.contrib.auth.mixins import LoginRequiredMixin
from django.views.generic import TemplateView
from django.utils import timezone
from django.db.models import Count, Avg, Q, Sum

urlpatterns = []


class ReportsDashboardView(LoginRequiredMixin, TemplateView):
    template_name = 'reports/dashboard.html'


class TaskReportView(LoginRequiredMixin, TemplateView):
    template_name = 'reports/task_report.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        from apps.tasks.models import Task
        now = timezone.now()
        ctx['tasks_by_status'] = Task.objects.values('status').annotate(count=Count('id'))
        ctx['tasks_by_priority'] = Task.objects.values('priority').annotate(count=Count('id'))
        ctx['overdue_count'] = Task.objects.filter(
            status__in=['todo','in_progress'], due_date__lt=now
        ).count()
        ctx['completed_this_month'] = Task.objects.filter(
            status='done', completed_at__month=now.month, completed_at__year=now.year
        ).count()
        return ctx


class ProjectReportView(LoginRequiredMixin, TemplateView):
    template_name = 'reports/project_report.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        from apps.projects.models import Project
        ctx['projects_by_status'] = Project.objects.values('status').annotate(count=Count('id'))
        ctx['avg_progress'] = Project.objects.filter(status='active').aggregate(
            avg=Avg('progress_pct')
        )['avg'] or 0
        ctx['total_budget'] = Project.objects.filter(
            status='active'
        ).aggregate(t=Sum('budget'))['t'] or 0
        return ctx


class StaffReportView(LoginRequiredMixin, TemplateView):
    template_name = 'reports/staff_report.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        from apps.core.models import User
        from apps.staff.models import LeaveRequest
        ctx['staff_by_dept'] = User.objects.filter(
            is_active=True, status='active'
        ).values('staffprofile__department__name').annotate(count=Count('id'))
        ctx['leave_by_type'] = LeaveRequest.objects.filter(
            status='approved'
        ).values('leave_type__name').annotate(count=Count('id'))
        return ctx


class FinanceReportView(LoginRequiredMixin, TemplateView):
    template_name = 'reports/finance_report.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        from apps.finance.models import Budget, Payment
        year = timezone.now().year
        budgets = Budget.objects.filter(fiscal_year=year)
        ctx['budget_summary'] = budgets.aggregate(
            total=Sum('total_amount'), spent=Sum('spent_amount')
        )
        ctx['budgets'] = budgets.select_related('department')
        ctx['payments_this_year'] = Payment.objects.filter(
            payment_date__year=year
        ).aggregate(total=Sum('amount'))['total'] or 0
        return ctx
