"""
apps/reports/views.py
─────────────────────
ALL reports require CEO or Superuser. HR/Finance/everyone else is blocked.

The dispatch-level gate is duplicated across each view (rather than a
single mixin in this file) so each view can be moved/re-exported
independently without anyone forgetting the guard. The cost is one
extra method per class — cheaper than a security incident.
"""

import json
from datetime import timedelta
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import HttpResponseForbidden
from django.views.generic import TemplateView
from django.utils import timezone
from django.db.models import Count, Avg, Q, Sum
from django.db.models.functions import TruncDate

# Re-use the role helpers already defined in apps/finance/views.py so we
# have ONE source of truth for "what is a CEO" across the codebase.
from apps.finance.views import _is_ceo


# ---------------------------------------------------------------------------
# Permission helper
# ---------------------------------------------------------------------------

def _can_view_reports(user):
    """
    Reports = strategic / cross-org data. Restricted to CEO + superusers.

    *_is_ceo* already returns True for superusers, members of the 'CEO'
    group, and users whose StaffProfile.position.title is 'CEO'. That
    matches the exact scope the user asked for, so we just delegate.
    """
    if not user or not getattr(user, 'is_authenticated', False):
        return False
    return _is_ceo(user)


class _ReportsAccessMixin(LoginRequiredMixin):
    """
    Drop-in dispatch gate for every reports view. Returns 403 (not a
    redirect) when a non-CEO/non-superuser hits the URL directly — even
    if they typed it manually or followed a stale link.
    """

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated and not _can_view_reports(request.user):
            return HttpResponseForbidden(
                'Reports are restricted to CEO and Superuser accounts.'
            )
        return super().dispatch(request, *args, **kwargs)


# ---------------------------------------------------------------------------
# Helper: humanise the AuditLog.model_name into "app / module" labels.
#
# AuditLog stores model_name as a free-form string (whatever the caller
# passed). In practice it tends to look like "apps.tasks.Task" or just
# "Task". We normalise to a friendlier app label so the chart isn't a
# wall of dotted paths.
# ---------------------------------------------------------------------------

_APP_LABELS = {
    'tasks': 'Tasks',
    'projects': 'Projects',
    'staff': 'Staff / HR',
    'finance': 'Finance',
    'core': 'Core / Auth',
    'reports': 'Reports',
    'documents': 'Documents',
    'leave': 'Leave',
    'payroll': 'Payroll',
    'attendance': 'Attendance',
    'inventory': 'Inventory',
}


def _app_from_model_name(model_name: str) -> str:
    """Best-effort: map 'apps.tasks.Task' -> 'Tasks'. Falls back to raw."""
    if not model_name:
        return 'Unknown'
    parts = model_name.split('.')
    # 'apps.tasks.Task' -> 'tasks'
    if len(parts) >= 2 and parts[0] == 'apps':
        key = parts[1].lower()
    elif len(parts) >= 2:
        key = parts[-2].lower()
    else:
        key = model_name.lower()
    return _APP_LABELS.get(key, key.replace('_', ' ').title())


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------

class ReportsDashboardView(_ReportsAccessMixin, TemplateView):
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

        # ── Security snapshot (last 24h) ─────────────────────────────────
        try:
            from apps.core.models import SecurityEvent
            since_24h = now - timedelta(hours=24)
            failed_types = ['login_failed', 'otp_failed', 'untrusted_attempt',
                            'geo_alert', 'account_locked']
            ctx['security_alerts_24h'] = SecurityEvent.objects.filter(
                created_at__gte=since_24h, event_type__in=failed_types
            ).count()
        except Exception:
            ctx['security_alerts_24h'] = 0

        # ── Usage snapshot (today) ───────────────────────────────────────
        try:
            from apps.core.models import AuditLog
            today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            ctx['active_users_today'] = (
                AuditLog.objects.filter(timestamp__gte=today_start)
                .values('user_id').distinct().count()
            )
            top = (
                AuditLog.objects.filter(timestamp__gte=now - timedelta(days=30))
                .values('model_name').annotate(c=Count('id')).order_by('-c').first()
            )
            ctx['top_app_30d'] = _app_from_model_name(top['model_name']) if top else '—'
        except Exception:
            ctx['active_users_today'] = 0
            ctx['top_app_30d'] = '—'

        return ctx


class TaskReportView(_ReportsAccessMixin, TemplateView):
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
        ctx['overdue_tasks'] = Task.objects.filter(
            status__in=['todo', 'in_progress'], due_date__lt=now
        ).select_related('assigned_to').order_by('due_date')[:10]
        return ctx


class ProjectReportView(_ReportsAccessMixin, TemplateView):
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


class StaffReportView(_ReportsAccessMixin, TemplateView):
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


class FinanceReportView(_ReportsAccessMixin, TemplateView):
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


# ---------------------------------------------------------------------------
# NEW: Security Report
# ---------------------------------------------------------------------------

class SecurityReportView(_ReportsAccessMixin, TemplateView):
    """
    Security posture overview. Pulls from SecurityEvent.

    Charts:
      • Events by type (donut)            — what's happening
      • Login success vs failed (30d)     — trust signal
      • Top users with failed logins      — accounts under attack
      • Geo / untrusted alerts (table)    — incidents to investigate
    """
    template_name = 'reports/security_report.html'

    # Anything in this set is treated as a "concerning" signal in the KPIs.
    ALERT_TYPES = (
        'login_failed', 'otp_failed', 'otp_expired',
        'account_locked', 'untrusted_attempt', 'geo_alert',
    )

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        from apps.core.models import SecurityEvent

        now = timezone.now()
        since_24h = now - timedelta(hours=24)
        since_7d  = now - timedelta(days=7)
        since_30d = now - timedelta(days=30)

        # ── Top-line KPIs ────────────────────────────────────────────────
        ctx['total_events_30d']    = SecurityEvent.objects.filter(created_at__gte=since_30d).count()
        ctx['failed_logins_24h']   = SecurityEvent.objects.filter(
            created_at__gte=since_24h, event_type='login_failed'
        ).count()
        ctx['locked_accounts_30d'] = SecurityEvent.objects.filter(
            created_at__gte=since_30d, event_type='account_locked'
        ).count()
        ctx['geo_alerts_30d']      = SecurityEvent.objects.filter(
            created_at__gte=since_30d, event_type='geo_alert'
        ).count()
        ctx['untrusted_attempts_30d'] = SecurityEvent.objects.filter(
            created_at__gte=since_30d, event_type='untrusted_attempt'
        ).count()
        ctx['successful_logins_30d'] = SecurityEvent.objects.filter(
            created_at__gte=since_30d, event_type='login_success'
        ).count()

        # ── Events by type (last 30d) — donut chart data ────────────────
        events_by_type = list(
            SecurityEvent.objects.filter(created_at__gte=since_30d)
            .values('event_type').annotate(count=Count('id')).order_by('-count')
        )
        # Friendly labels mirroring SecurityEvent.EventType.choices.
        # Cast to str() — choices use gettext_lazy and the proxies are not
        # JSON-serialisable.
        type_labels = {k: str(v) for k, v in SecurityEvent.EventType.choices}
        for row in events_by_type:
            row['label'] = type_labels.get(row['event_type'], row['event_type'])
        ctx['events_by_type'] = events_by_type
        ctx['events_by_type_json'] = json.dumps(events_by_type)

        # ── Daily login success vs failed (last 30d) — line chart ───────
        daily_qs = (
            SecurityEvent.objects
            .filter(created_at__gte=since_30d, event_type__in=['login_success', 'login_failed'])
            .annotate(d=TruncDate('created_at'))
            .values('d', 'event_type')
            .annotate(c=Count('id'))
            .order_by('d')
        )
        # Pivot into {date -> {success: n, failed: n}}
        bucket = {}
        for row in daily_qs:
            day = row['d'].isoformat() if row['d'] else 'unknown'
            bucket.setdefault(day, {'success': 0, 'failed': 0})
            key = 'success' if row['event_type'] == 'login_success' else 'failed'
            bucket[day][key] = row['c']
        login_trend = [
            {'date': d, 'success': v['success'], 'failed': v['failed']}
            for d, v in sorted(bucket.items())
        ]
        ctx['login_trend_json'] = json.dumps(login_trend)

        # ── Top users with failed logins (last 30d) ─────────────────────
        top_failed = list(
            SecurityEvent.objects.filter(
                created_at__gte=since_30d, event_type='login_failed'
            )
            .values('user__email', 'user__first_name', 'user__last_name')
            .annotate(count=Count('id'))
            .order_by('-count')[:10]
        )
        ctx['top_failed_users'] = top_failed

        # ── Recent alert events for the table (last 7d, top 25) ─────────
        ctx['recent_alerts'] = (
            SecurityEvent.objects
            .filter(created_at__gte=since_7d, event_type__in=self.ALERT_TYPES)
            .select_related('user')
            .order_by('-created_at')[:25]
        )

        # ── Top countries by event volume (last 30d) ────────────────────
        ctx['top_countries'] = list(
            SecurityEvent.objects.filter(
                created_at__gte=since_30d
            ).exclude(country='')
            .values('country').annotate(count=Count('id'))
            .order_by('-count')[:8]
        )

        return ctx


# ---------------------------------------------------------------------------
# NEW: Usage Report — "most used apps", action mix, active users
# ---------------------------------------------------------------------------

class UsageReportView(_ReportsAccessMixin, TemplateView):
    """
    System usage analytics, derived from AuditLog.

    Charts:
      • Most-used apps / modules (bar)        — by AuditLog.model_name
      • Action mix (donut)                    — create / update / delete / etc.
      • Daily activity trend (line, 30d)      — heartbeat of the system
      • Top active users (table)              — power users
      • Recent activity (table)               — what just happened
    """
    template_name = 'reports/usage_report.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        from apps.core.models import AuditLog

        now = timezone.now()
        since_24h = now - timedelta(hours=24)
        since_7d  = now - timedelta(days=7)
        since_30d = now - timedelta(days=30)

        # ── Top-line KPIs ────────────────────────────────────────────────
        ctx['total_actions_30d'] = AuditLog.objects.filter(timestamp__gte=since_30d).count()
        ctx['actions_today']     = AuditLog.objects.filter(timestamp__gte=since_24h).count()
        ctx['active_users_30d']  = (
            AuditLog.objects.filter(timestamp__gte=since_30d)
            .values('user_id').distinct().count()
        )
        ctx['active_users_7d']   = (
            AuditLog.objects.filter(timestamp__gte=since_7d)
            .values('user_id').distinct().count()
        )

        # ── Most-used apps (bar) ─────────────────────────────────────────
        # Group raw model_name first, then bucket into app labels in Python
        # because model_name is a free-form CharField.
        raw_models = (
            AuditLog.objects.filter(timestamp__gte=since_30d)
            .values('model_name').annotate(count=Count('id'))
        )
        app_totals = {}
        for row in raw_models:
            label = _app_from_model_name(row['model_name'])
            app_totals[label] = app_totals.get(label, 0) + row['count']
        apps_sorted = sorted(
            ({'app': k, 'count': v} for k, v in app_totals.items()),
            key=lambda r: r['count'], reverse=True,
        )
        ctx['apps_by_usage']      = apps_sorted[:12]
        ctx['apps_by_usage_json'] = json.dumps(apps_sorted[:12])

        # ── Action mix (donut) ───────────────────────────────────────────
        # Cast labels to str() — same lazy-proxy reason as in the security
        # report. dict(...choices) would otherwise leak __proxy__ objects.
        action_choices = {k: str(v) for k, v in AuditLog.Action.choices}
        actions = list(
            AuditLog.objects.filter(timestamp__gte=since_30d)
            .values('action').annotate(count=Count('id')).order_by('-count')
        )
        for row in actions:
            row['label'] = action_choices.get(row['action'], row['action'].title())
        ctx['actions_breakdown']      = actions
        ctx['actions_breakdown_json'] = json.dumps(actions)

        # ── Daily activity (last 30d) ────────────────────────────────────
        daily = list(
            AuditLog.objects.filter(timestamp__gte=since_30d)
            .annotate(d=TruncDate('timestamp'))
            .values('d').annotate(count=Count('id'))
            .order_by('d')
        )
        ctx['daily_activity_json'] = json.dumps([
            {'date': r['d'].isoformat() if r['d'] else '', 'count': r['count']}
            for r in daily
        ])

        # ── Top active users (last 30d) ──────────────────────────────────
        ctx['top_users'] = list(
            AuditLog.objects.filter(timestamp__gte=since_30d, user__isnull=False)
            .values('user__email', 'user__first_name', 'user__last_name')
            .annotate(count=Count('id'))
            .order_by('-count')[:10]
        )

        # ── Recent activity (last 50, regardless of window) ──────────────
        ctx['recent_activity'] = (
            AuditLog.objects.select_related('user')
            .order_by('-timestamp')[:50]
        )

        return ctx