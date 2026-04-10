import json
from django.contrib.auth.mixins import LoginRequiredMixin
from django.views.generic import TemplateView
from django.utils import timezone
from django.db.models import Count, Avg, Q, Sum


class ReportsDashboardView(LoginRequiredMixin, TemplateView):
    template_name = 'reports/dashboard.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        now = timezone.now()
        try:
            from apps.tasks.models import Task
            ctx['total_tasks'] = Task.objects.count()
            ctx['overdue_tasks'] = Task.objects.filter(
                status__in=['todo', 'in_progress'], due_date__lt=now
            ).count()
            ctx['tasks_done_month'] = Task.objects.filter(
                status='done', completed_at__month=now.month, completed_at__year=now.year
            ).count()
        except Exception:
            ctx['total_tasks'] = ctx['overdue_tasks'] = ctx['tasks_done_month'] = 0

        try:
            from apps.projects.models import Project
            ctx['total_projects'] = Project.objects.filter(status='active').count()
            ctx['avg_progress'] = round(
                Project.objects.filter(status='active').aggregate(a=Avg('progress_pct'))['a'] or 0
            )
        except Exception:
            ctx['total_projects'] = ctx['avg_progress'] = 0

        try:
            from apps.core.models import User
            ctx['total_staff'] = User.objects.filter(is_active=True, status='active').count()
        except Exception:
            ctx['total_staff'] = 0

        try:
            from apps.finance.models import Budget
            year = now.year
            budgets = Budget.objects.filter(fiscal_year=year).aggregate(
                total=Sum('total_amount'), spent=Sum('spent_amount')
            )
            ctx['budget_total'] = budgets['total'] or 0
            ctx['budget_spent'] = budgets['spent'] or 0
        except Exception:
            ctx['budget_total'] = ctx['budget_spent'] = 0

        return ctx


class TaskReportView(LoginRequiredMixin, TemplateView):
    template_name = 'reports/task_report.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        from apps.tasks.models import Task
        now = timezone.now()
        ctx['tasks_by_status_json'] = json.dumps(list(Task.objects.values('status').annotate(count=Count('id'))))
        ctx['tasks_by_priority_json'] = json.dumps(list(Task.objects.values('priority').annotate(count=Count('id'))))
        ctx['overdue_count'] = Task.objects.filter(
            status__in=['todo', 'in_progress'], due_date__lt=now
        ).count()
        ctx['completed_this_month'] = Task.objects.filter(
            status='done', completed_at__month=now.month, completed_at__year=now.year
        ).count()
        ctx['total_tasks'] = Task.objects.count()
        ctx['in_progress_count'] = Task.objects.filter(status='in_progress').count()
        # Recent overdue tasks
        ctx['overdue_tasks'] = Task.objects.filter(
            status__in=['todo', 'in_progress'], due_date__lt=now
        ).select_related('assigned_to').order_by('due_date')[:10]
        return ctx


class ProjectReportView(LoginRequiredMixin, TemplateView):
    template_name = 'reports/project_report.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        from apps.projects.models import Project
        ctx['projects_by_status_json'] = json.dumps(list(Project.objects.values('status').annotate(count=Count('id'))))
        ctx['avg_progress'] = round(
            Project.objects.filter(status='active').aggregate(avg=Avg('progress_pct'))['avg'] or 0
        )
        ctx['total_budget'] = Project.objects.filter(
            status='active'
        ).aggregate(t=Sum('budget'))['t'] or 0
        ctx['total_projects'] = Project.objects.count()
        ctx['active_projects'] = Project.objects.filter(status='active').count()
        ctx['projects'] = Project.objects.select_related("project_manager").order_by('-progress_pct')[:15]
        return ctx


class StaffReportView(LoginRequiredMixin, TemplateView):
    template_name = 'reports/staff_report.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        from apps.core.models import User
        from apps.staff.models import LeaveRequest
        staff_by_dept = list(
            User.objects.filter(is_active=True, status='active')
            .values('staffprofile__department__name')
            .annotate(count=Count('id'))
            .order_by('-count')
        )
        leave_by_type = list(
            LeaveRequest.objects.filter(status='approved')
            .values('leave_type__name')
            .annotate(count=Count('id'))
            .order_by('-count')
        )
        ctx['staff_by_dept'] = staff_by_dept
        ctx['leave_by_type'] = leave_by_type
        ctx['staff_by_dept_json'] = json.dumps(staff_by_dept)
        ctx['leave_by_type_json'] = json.dumps(leave_by_type)
        ctx['total_staff'] = User.objects.filter(is_active=True, status='active').count()
        ctx['pending_leaves'] = LeaveRequest.objects.filter(status='pending').count()
        ctx['approved_leaves'] = LeaveRequest.objects.filter(status='approved').count()
        return ctx


class FinanceReportView(LoginRequiredMixin, TemplateView):
    template_name = 'reports/finance_report.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        from apps.finance.models import Budget, Payment
        year = timezone.now().year
        budgets = Budget.objects.filter(fiscal_year=year)
        summary = budgets.aggregate(total=Sum('total_amount'), spent=Sum('spent_amount'))
        ctx['budget_total'] = summary['total'] or 0
        ctx['budget_spent'] = summary['spent'] or 0
        ctx['budget_remaining'] = (summary['total'] or 0) - (summary['spent'] or 0)
        ctx['budget_pct'] = round(
            (ctx['budget_spent'] / ctx['budget_total'] * 100) if ctx['budget_total'] else 0
        )
        ctx['budgets'] = budgets.select_related('department').order_by('-total_amount')
        ctx['payments_this_year'] = Payment.objects.filter(
            payment_date__year=year
        ).aggregate(total=Sum('amount'))['total'] or 0
        payments_by_month = list(
            Payment.objects.filter(payment_date__year=year)
            .values('payment_date__month')
            .annotate(total=Sum('amount'))
            .order_by('payment_date__month')
        )
        ctx['payments_by_month'] = payments_by_month
        ctx['payments_by_month_json'] = json.dumps(
            [{'payment_date__month': r['payment_date__month'], 'total': float(r['total'] or 0)} for r in payments_by_month]
        )
        ctx['year'] = year
        return ctx