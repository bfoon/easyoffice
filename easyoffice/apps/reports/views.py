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
from django.db.models.functions import TruncDate, TruncMonth

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
# Broader permission helper for "supervisor / admin" reports
# (file storage, invoicing). Wider audience than the strategic CEO reports.
# ---------------------------------------------------------------------------

# Group-name and position-title keywords that grant supervisor-tier access.
# Match is case-insensitive substring on the position title and case-insensitive
# exact on group name.
_SUPERVISOR_GROUPS = {'supervisor', 'admin', 'administrator', 'ceo', 'manager'}
_SUPERVISOR_TITLE_KEYWORDS = ('supervisor', 'manager', 'admin', 'ceo', 'head of')


def _can_view_supervisor_reports(user):
    """
    Permissive check for the supervisor/admin tier of reports.

    Passes if ANY of:
      • user.is_superuser
      • user.is_staff (Django admin staff flag)
      • user is in a group named Supervisor / Admin / Administrator / CEO / Manager
      • user.staffprofile.position.title contains 'supervisor', 'manager',
        'admin', 'ceo', or 'head of'  (case-insensitive)

    Designed not to crash if the user has no staff profile / no position.
    """
    if not user or not getattr(user, 'is_authenticated', False):
        return False

    # Superuser and Django staff flag — fastest paths
    if getattr(user, 'is_superuser', False) or getattr(user, 'is_staff', False):
        return True

    # Group membership
    try:
        if user.groups.filter(name__iregex=r'^(supervisor|admin|administrator|ceo|manager)$').exists():
            return True
    except Exception:
        pass

    # StaffProfile.position.title — guarded against missing profile/position
    try:
        title = (user.staffprofile.position.title or '').lower()
        if any(kw in title for kw in _SUPERVISOR_TITLE_KEYWORDS):
            return True
    except Exception:
        pass

    return False


class _SupervisorReportsAccessMixin(LoginRequiredMixin):
    """
    Dispatch gate for reports open to supervisors + admins (broader than
    the CEO-only mixin above). Returns a 403 — not a redirect — so a
    misconfigured link surfaces clearly instead of silently bouncing.
    """

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated and not _can_view_supervisor_reports(request.user):
            return HttpResponseForbidden(
                'This report is restricted to Supervisor and Admin accounts.'
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

        # ── Storage snapshot ─────────────────────────────────────────────
        try:
            from apps.files.models import SharedFile
            agg = SharedFile.objects.aggregate(total=Sum('file_size'), n=Count('id'))
            ctx['storage_total_bytes'] = agg['total'] or 0
            ctx['storage_file_count']  = agg['n'] or 0
        except Exception:
            ctx['storage_total_bytes'] = 0
            ctx['storage_file_count']  = 0

        # ── Invoicing snapshot (this month) ──────────────────────────────
        try:
            from apps.invoices.models import InvoiceDocument
            month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            month_qs = InvoiceDocument.objects.filter(
                status='finalized', finalized_at__gte=month_start,
            )
            ctx['invoices_this_month_count'] = month_qs.count()
            ctx['invoices_this_month_total'] = month_qs.aggregate(t=Sum('total'))['t'] or 0
        except Exception:
            ctx['invoices_this_month_count'] = 0
            ctx['invoices_this_month_total'] = 0

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


# ---------------------------------------------------------------------------
# Helpers shared by the storage report
# ---------------------------------------------------------------------------

# Mirrors apps/files/models.py::_TYPE_CATEGORY so the report buckets look
# identical to what users see elsewhere in the app. Re-defined here (rather
# than imported) to keep the reports app decoupled — the cost is one
# duplicated dict, the win is no circular-import surprises.
_FILE_TYPE_CATEGORY = {
    'document':     {'doc', 'docx', 'pdf', 'txt', 'md', 'rtf', 'odt'},
    'spreadsheet':  {'xls', 'xlsx', 'csv', 'ods'},
    'presentation': {'ppt', 'pptx', 'odp'},
    'image':        {'jpg', 'jpeg', 'png', 'gif', 'webp', 'svg', 'bmp', 'tiff'},
    'video':        {'mp4', 'mov', 'avi', 'mkv', 'webm'},
    'audio':        {'mp3', 'wav', 'ogg', 'm4a'},
    'archive':      {'zip', 'rar', '7z', 'tar', 'gz'},
    'code':         {'py', 'js', 'html', 'css', 'json', 'xml', 'sql'},
}


def _category_for_extension(ext: str) -> str:
    """Map 'pdf' -> 'document', 'mp4' -> 'video', unknown -> 'other'."""
    ext = (ext or '').lower().lstrip('.')
    for cat, exts in _FILE_TYPE_CATEGORY.items():
        if ext in exts:
            return cat
    return 'other'


def _humanise_bytes(n: int) -> str:
    """1234567 -> '1.2 MB'. Used for tables/KPIs; chart datasets stay in bytes."""
    n = int(n or 0)
    for unit in ('B', 'KB', 'MB', 'GB'):
        if n < 1024:
            return f'{n:.0f} {unit}' if unit == 'B' else f'{n:.1f} {unit}'
        n /= 1024
    return f'{n:.1f} TB'


# ---------------------------------------------------------------------------
# NEW: File Storage Report (Supervisor / Admin)
# ---------------------------------------------------------------------------

class FileStorageReportView(_SupervisorReportsAccessMixin, TemplateView):
    """
    Storage analytics derived from apps.files.SharedFile.

    Charts:
      • Total storage (KPI) + file count + avg size
      • Storage by file type / category (donut + breakdown)
      • Top users by storage consumed (bar)
      • Storage by visibility (private / shared / department / office)
      • Monthly upload trend (line, last 12 months) — count and bytes
      • Largest files (table)
    """
    template_name = 'reports/file_storage_report.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        from apps.files.models import SharedFile

        now = timezone.now()
        twelve_months_ago = (now.replace(day=1) - timedelta(days=365)).replace(day=1)

        # ── Top-line KPIs ────────────────────────────────────────────────
        agg = SharedFile.objects.aggregate(
            total_bytes=Sum('file_size'),
            file_count=Count('id'),
            total_downloads=Sum('download_count'),
        )
        total_bytes = agg['total_bytes'] or 0
        file_count  = agg['file_count'] or 0
        ctx['total_bytes']      = total_bytes
        ctx['total_human']      = _humanise_bytes(total_bytes)
        ctx['file_count']       = file_count
        ctx['total_downloads']  = agg['total_downloads'] or 0
        ctx['avg_size_human']   = _humanise_bytes(total_bytes // file_count) if file_count else '0 B'

        # Trashed files — useful operational signal (recoverable storage)
        try:
            from apps.files.models import FileTrash
            trash_agg = FileTrash.objects.aggregate(c=Count('id'))
            ctx['trashed_count'] = trash_agg['c'] or 0
        except Exception:
            ctx['trashed_count'] = 0

        # ── Storage by file type / category ──────────────────────────────
        # SharedFile has a .file_type CharField but it's not always populated;
        # the rest of the app uses the filename extension via _TYPE_CATEGORY.
        # We mirror that, doing the bucketing in Python because we need to
        # parse the .name extension which the DB can't index efficiently.
        rows = SharedFile.objects.values('name', 'file_size')
        by_category = {cat: {'bytes': 0, 'count': 0} for cat in _FILE_TYPE_CATEGORY}
        by_category['other'] = {'bytes': 0, 'count': 0}
        by_extension = {}  # for the long-tail extension breakdown table

        for r in rows:
            name = r['name'] or ''
            size = r['file_size'] or 0
            ext = name.rsplit('.', 1)[-1].lower() if '.' in name else ''
            cat = _category_for_extension(ext)
            by_category[cat]['bytes'] += size
            by_category[cat]['count'] += 1
            if ext:
                by_extension.setdefault(ext, {'bytes': 0, 'count': 0})
                by_extension[ext]['bytes'] += size
                by_extension[ext]['count'] += 1

        # Sort categories by bytes desc, drop empties
        cat_data = sorted(
            ({'category': k.title(), 'bytes': v['bytes'], 'count': v['count'],
              'human': _humanise_bytes(v['bytes'])}
             for k, v in by_category.items() if v['count'] > 0),
            key=lambda r: r['bytes'], reverse=True,
        )
        ctx['by_category']      = cat_data
        ctx['by_category_json'] = json.dumps(cat_data)

        # Top 12 extensions for the granular breakdown
        ext_data = sorted(
            ({'ext': k, 'bytes': v['bytes'], 'count': v['count'],
              'human': _humanise_bytes(v['bytes'])}
             for k, v in by_extension.items()),
            key=lambda r: r['bytes'], reverse=True,
        )[:12]
        ctx['by_extension'] = ext_data

        # ── Top users by storage consumed ────────────────────────────────
        top_users = list(
            SharedFile.objects.values(
                'uploaded_by__email',
                'uploaded_by__first_name',
                'uploaded_by__last_name',
            )
            .annotate(bytes=Sum('file_size'), files=Count('id'))
            .order_by('-bytes')[:10]
        )
        for u in top_users:
            u['human'] = _humanise_bytes(u['bytes'] or 0)
        ctx['top_users']      = top_users
        ctx['top_users_json'] = json.dumps([
            {
                'name': (
                    f"{u['uploaded_by__first_name'] or ''} {u['uploaded_by__last_name'] or ''}".strip()
                    or u['uploaded_by__email'] or 'Unknown'
                ),
                'bytes': int(u['bytes'] or 0),
                'files': u['files'],
            }
            for u in top_users
        ])

        # ── Storage by visibility ────────────────────────────────────────
        vis_choices = {k: str(v) for k, v in SharedFile.Visibility.choices}
        vis_rows = list(
            SharedFile.objects.values('visibility')
            .annotate(bytes=Sum('file_size'), count=Count('id'))
            .order_by('-bytes')
        )
        for r in vis_rows:
            r['label'] = vis_choices.get(r['visibility'], r['visibility'])
            r['human'] = _humanise_bytes(r['bytes'] or 0)
        ctx['by_visibility']      = vis_rows
        ctx['by_visibility_json'] = json.dumps([
            {'label': r['label'], 'bytes': int(r['bytes'] or 0), 'count': r['count']}
            for r in vis_rows
        ])

        # ── Monthly upload trend (last 12 months) ────────────────────────
        monthly = list(
            SharedFile.objects.filter(created_at__gte=twelve_months_ago)
            .annotate(m=TruncMonth('created_at'))
            .values('m')
            .annotate(count=Count('id'), bytes=Sum('file_size'))
            .order_by('m')
        )
        ctx['monthly_uploads_json'] = json.dumps([
            {
                'month': r['m'].strftime('%Y-%m') if r['m'] else '',
                'count': r['count'],
                'bytes': int(r['bytes'] or 0),
                'mb': round((r['bytes'] or 0) / (1024 * 1024), 2),
            }
            for r in monthly
        ])

        # ── Largest files (top 15) ───────────────────────────────────────
        ctx['largest_files'] = (
            SharedFile.objects.select_related('uploaded_by')
            .order_by('-file_size')[:15]
        )

        return ctx


# ---------------------------------------------------------------------------
# NEW: Invoicing Report (Supervisor / Admin)
#   Counts + totals for Invoice / Proforma / Delivery Note across daily,
#   monthly windows, plus status mix and top clients.
# ---------------------------------------------------------------------------

class InvoicingReportView(_SupervisorReportsAccessMixin, TemplateView):
    template_name = 'reports/invoicing_report.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        from apps.invoices.models import InvoiceDocument, DocType

        now = timezone.now()
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        twelve_months_ago = (now.replace(day=1) - timedelta(days=365)).replace(day=1)
        thirty_days_ago = now - timedelta(days=30)

        # We build all KPI cards from FINALIZED documents — drafts and
        # voided docs aren't real revenue. Status mix is reported separately.
        finalized = InvoiceDocument.objects.filter(status='finalized')

        # ── Doc type labels (resolve lazy proxies for JSON) ──────────────
        doctype_labels = {k: str(v) for k, v in DocType.choices}

        # ── KPI per doc type: count + total this month + today ───────────
        # Build a dict keyed by doc_type so the template can look it up
        # cleanly without nested {% if %} chains.
        per_type = {}
        for dt_value, dt_label in DocType.choices:
            month_qs = finalized.filter(doc_type=dt_value, finalized_at__gte=month_start)
            today_qs = finalized.filter(doc_type=dt_value, finalized_at__gte=today_start)
            ytd_qs   = finalized.filter(doc_type=dt_value, finalized_at__year=now.year)
            per_type[dt_value] = {
                'label': str(dt_label),
                'count_month': month_qs.count(),
                'total_month': month_qs.aggregate(t=Sum('total'))['t'] or 0,
                'count_today': today_qs.count(),
                'total_today': today_qs.aggregate(t=Sum('total'))['t'] or 0,
                'count_ytd':   ytd_qs.count(),
                'total_ytd':   ytd_qs.aggregate(t=Sum('total'))['t'] or 0,
            }
        ctx['per_type'] = per_type

        # Convenience top-line totals (sum across all doc types, this month)
        ctx['grand_count_month'] = sum(v['count_month'] for v in per_type.values())
        ctx['grand_total_month'] = sum(v['total_month'] for v in per_type.values())
        ctx['grand_count_today'] = sum(v['count_today'] for v in per_type.values())
        ctx['grand_total_today'] = sum(v['total_today'] for v in per_type.values())

        # ── Monthly trend (last 12 months) per doc type ──────────────────
        monthly = list(
            finalized.filter(finalized_at__gte=twelve_months_ago)
            .annotate(m=TruncMonth('finalized_at'))
            .values('m', 'doc_type')
            .annotate(count=Count('id'), total=Sum('total'))
            .order_by('m')
        )
        # Pivot into one entry per month with each doc_type as a column.
        # Chart.js expects parallel arrays, so we'll emit a tidy structure
        # the template script can iterate.
        month_buckets = {}
        for r in monthly:
            mkey = r['m'].strftime('%Y-%m') if r['m'] else 'unknown'
            bucket = month_buckets.setdefault(mkey, {dt: {'count': 0, 'total': 0.0} for dt, _ in DocType.choices})
            bucket[r['doc_type']] = {
                'count': r['count'],
                'total': float(r['total'] or 0),
            }
        monthly_series = [
            {
                'month': m,
                'series': month_buckets[m],
            }
            for m in sorted(month_buckets.keys())
        ]
        ctx['monthly_series_json'] = json.dumps(monthly_series)
        # Echo the doc-type list so the JS can build series in a stable order.
        ctx['doc_types_json'] = json.dumps([
            {'value': dt, 'label': doctype_labels[dt]} for dt, _ in DocType.choices
        ])

        # ── Daily trend (last 30 days) — totals only, all doc types ─────
        daily = list(
            finalized.filter(finalized_at__gte=thirty_days_ago)
            .annotate(d=TruncDate('finalized_at'))
            .values('d')
            .annotate(count=Count('id'), total=Sum('total'))
            .order_by('d')
        )
        ctx['daily_series_json'] = json.dumps([
            {
                'date': r['d'].isoformat() if r['d'] else '',
                'count': r['count'],
                'total': float(r['total'] or 0),
            }
            for r in daily
        ])

        # ── Status mix (donut) — across ALL documents, not just finalized ─
        status_choices = {k: str(v) for k, v in InvoiceDocument.Status.choices}
        status_rows = list(
            InvoiceDocument.objects.values('status')
            .annotate(count=Count('id'))
            .order_by('-count')
        )
        for r in status_rows:
            r['label'] = status_choices.get(r['status'], r['status'])
        ctx['status_mix']      = status_rows
        ctx['status_mix_json'] = json.dumps(status_rows)

        # ── Top clients by total billed (finalized only, this year) ──────
        top_clients = list(
            finalized.filter(finalized_at__year=now.year)
            .exclude(client_name='')
            .values('client_name')
            .annotate(count=Count('id'), total=Sum('total'))
            .order_by('-total')[:10]
        )
        ctx['top_clients'] = top_clients

        # ── Recent finalized docs (table) ────────────────────────────────
        ctx['recent_docs'] = (
            InvoiceDocument.objects
            .filter(status='finalized')
            .select_related('created_by')
            .order_by('-finalized_at')[:20]
        )

        ctx['currency'] = 'GMD'  # default currency on the model
        return ctx