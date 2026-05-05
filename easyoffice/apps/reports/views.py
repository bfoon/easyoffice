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
from statistics import median
from datetime import timedelta
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import HttpResponseForbidden
from django.views.generic import TemplateView
from django.utils import timezone
from django.db.models import Count, Avg, Q, Sum, F, Max, Min
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

        # ── Signature snapshot (lifetime) ────────────────────────────────
        # Powers the four signature KPI tiles on the dashboard. Wrapped in
        # try/excepts so a missing model never breaks the page.
        try:
            from apps.files.models import SignatureRequest
            sig_qs = SignatureRequest.objects.all()
            # "Signed via flow" = a full SignatureRequest reached terminal
            # 'completed' status (every signer signed via the email flow).
            ctx['sig_signed_flow'] = sig_qs.filter(status='completed').count()
            # "Pending" = anything in-flight: draft / sent / partial / viewed.
            # Anything that's NOT a terminal state counts as pending.
            ctx['sig_pending'] = sig_qs.exclude(
                status__in=['completed', 'cancelled', 'expired', 'declined']
            ).count()
            # "Voided / Cancelled" — DocuSign-ish terminology covering both
            # creator-cancelled requests and ones that aged out.
            ctx['sig_voided'] = sig_qs.filter(
                status__in=['cancelled', 'expired']
            ).count()
        except Exception:
            ctx['sig_signed_flow'] = 0
            ctx['sig_pending']     = 0
            ctx['sig_voided']      = 0

        # Quick-sign count — try a dedicated model first, fall back to
        # AuditLog records (most installs log a 'quick_sign' action).
        try:
            from apps.files.models import QuickSignSession
            ctx['sig_quick_signed'] = QuickSignSession.objects.filter(
                status='completed'
            ).count()
        except Exception:
            try:
                from apps.core.models import AuditLog
                ctx['sig_quick_signed'] = AuditLog.objects.filter(
                    action__in=['quick_sign', 'quick_signed', 'QuickSign']
                ).count()
            except Exception:
                ctx['sig_quick_signed'] = 0

        try:
            from apps.customer_service.models import (
                ServiceTicket as _CS_Ticket,
                CallRecord as _CS_Call,
            )
            today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            open_statuses = ['new', 'open', 'pending', 'assigned', 'in_progress',
                             'awaiting_customer', 'escalated']

            ctx['cs_calls_today'] = _CS_Call.objects.filter(
                started_at__gte=today_start
            ).count()
            ctx['cs_open_tickets'] = _CS_Ticket.objects.filter(
                status__in=open_statuses
            ).count()
            ctx['cs_overdue_tickets'] = _CS_Ticket.objects.filter(
                status__in=open_statuses,
                resolution_due_at__lt=now,
            ).count()
        except Exception:
            ctx['cs_calls_today'] = 0
            ctx['cs_open_tickets'] = 0
            ctx['cs_overdue_tickets'] = 0

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
      • Tokenised link usage (NEW)        — how many times each public
                                            link was opened, from where,
                                            and on what device.
    """
    template_name = 'reports/security_report.html'

    ALERT_TYPES = (
        'login_failed', 'otp_failed', 'otp_expired',
        'account_locked', 'untrusted_attempt', 'geo_alert',
    )

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        from apps.core.models import SecurityEvent

        now = timezone.now()
        since_24h = now - timedelta(hours=24)
        since_7d = now - timedelta(days=7)
        since_30d = now - timedelta(days=30)

        # ── Top-line KPIs ────────────────────────────────────────────────
        ctx['total_events_30d'] = SecurityEvent.objects.filter(created_at__gte=since_30d).count()
        ctx['failed_logins_24h'] = SecurityEvent.objects.filter(
            created_at__gte=since_24h, event_type='login_failed'
        ).count()
        ctx['locked_accounts_30d'] = SecurityEvent.objects.filter(
            created_at__gte=since_30d, event_type='account_locked'
        ).count()
        ctx['geo_alerts_30d'] = SecurityEvent.objects.filter(
            created_at__gte=since_30d, event_type='geo_alert'
        ).count()
        ctx['untrusted_attempts_30d'] = SecurityEvent.objects.filter(
            created_at__gte=since_30d, event_type='untrusted_attempt'
        ).count()
        ctx['successful_logins_30d'] = SecurityEvent.objects.filter(
            created_at__gte=since_30d, event_type='login_success'
        ).count()

        # ── Events by type donut ────────────────────────────────────────
        events_by_type = list(
            SecurityEvent.objects.filter(created_at__gte=since_30d)
            .values('event_type').annotate(count=Count('id')).order_by('-count')
        )
        type_labels = {k: str(v) for k, v in SecurityEvent.EventType.choices}
        for row in events_by_type:
            row['label'] = type_labels.get(row['event_type'], row['event_type'])
        ctx['events_by_type'] = events_by_type
        ctx['events_by_type_json'] = json.dumps(events_by_type)

        # ── Daily login success vs failed (30d) ─────────────────────────
        daily_qs = (
            SecurityEvent.objects
            .filter(created_at__gte=since_30d, event_type__in=['login_success', 'login_failed'])
            .annotate(d=TruncDate('created_at'))
            .values('d', 'event_type')
            .annotate(c=Count('id'))
            .order_by('d')
        )
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

        # ── Top failed-login users (30d) ────────────────────────────────
        ctx['top_failed_users'] = list(
            SecurityEvent.objects.filter(
                created_at__gte=since_30d, event_type='login_failed'
            )
            .values('user__email', 'user__first_name', 'user__last_name')
            .annotate(count=Count('id'))
            .order_by('-count')[:10]
        )

        # ── Recent alert events (7d, top 25) ────────────────────────────
        ctx['recent_alerts'] = (
            SecurityEvent.objects
            .filter(created_at__gte=since_7d, event_type__in=self.ALERT_TYPES)
            .select_related('user')
            .order_by('-created_at')[:25]
        )

        # ── Top countries by event volume (30d) ─────────────────────────
        ctx['top_countries'] = list(
            SecurityEvent.objects.filter(
                created_at__gte=since_30d
            ).exclude(country='')
            .values('country').annotate(count=Count('id'))
            .order_by('-count')[:8]
        )

        # ─────────────────────────────────────────────────────────────────
        # NEW: Tokenised public link usage
        # ─────────────────────────────────────────────────────────────────
        # We aggregate access events for THREE token-backed flows:
        #   1. apps.files.SignatureRequest   (sent to signers)
        #   2. apps.meetings.MeetingMinutesExternalReview (external review)
        #   3. apps.files.FilePublicToken    (30-day public download)
        #
        # Each is wrapped in try/except — if a tenant never deployed
        # the meetings module, that section just shows zero rows
        # instead of 500-ing the page.
        # ─────────────────────────────────────────────────────────────────
        token_usage_summary = {
            'signature_links': {'count': 0, 'opens': 0, 'unique_ips': 0},
            'meeting_links': {'count': 0, 'opens': 0, 'unique_ips': 0},
            'file_links': {'count': 0, 'opens': 0, 'unique_ips': 0},
        }
        token_link_rows = []  # most-recently-issued links across all types

        # 1. Signature request public links
        try:
            from apps.files.models import SignatureRequestSigner
            sig_signers = SignatureRequestSigner.objects.filter(
                request__created_at__gte=since_30d,
            ).select_related('request')
            for s in sig_signers[:200]:
                token_link_rows.append({
                    'kind': 'Signature',
                    'subject': getattr(s.request, 'subject', '') or
                               getattr(s.request, 'document_name', '') or
                               f'Request #{s.request_id}',
                    'recipient': getattr(s, 'email', '') or getattr(s, 'name', ''),
                    'open_count': getattr(s, 'view_count', 0) or 0,
                    'last_ip': getattr(s, 'last_ip', '') or getattr(s, 'ip_address', ''),
                    'last_device': getattr(s, 'last_user_agent', '') or
                                   getattr(s, 'device_name', ''),
                    'issued_at': getattr(s.request, 'created_at', None),
                    'expires_at': getattr(s.request, 'expires_at', None) or
                                  getattr(s, 'expires_at', None),
                    'status': getattr(s, 'status', '') or 'sent',
                })
            agg = sig_signers.aggregate(
                opens=Sum('view_count'),
            )
            token_usage_summary['signature_links']['count'] = sig_signers.count()
            token_usage_summary['signature_links']['opens'] = agg.get('opens') or 0
            token_usage_summary['signature_links']['unique_ips'] = (
                sig_signers.exclude(last_ip__isnull=True).exclude(last_ip='')
                .values('last_ip').distinct().count()
            )
        except Exception:
            pass

        # 2. Meeting minutes external review links
        try:
            from apps.meetings.models import MeetingMinutesExternalReview
            ext = MeetingMinutesExternalReview.objects.filter(
                created_at__gte=since_30d,
            ).select_related('minutes')
            for r in ext[:200]:
                token_link_rows.append({
                    'kind': 'Meeting Minutes',
                    'subject': getattr(r.minutes, 'subject', '') or
                               getattr(r.minutes, 'title', '') or
                               f'Minutes #{r.minutes_id}',
                    'recipient': getattr(r, 'email', '') or getattr(r, 'reviewer_email', ''),
                    'open_count': getattr(r, 'view_count', 0) or 0,
                    'last_ip': getattr(r, 'last_ip', '') or getattr(r, 'ip_address', ''),
                    'last_device': getattr(r, 'last_user_agent', '') or '',
                    'issued_at': getattr(r, 'created_at', None),
                    'expires_at': getattr(r, 'expires_at', None),
                    'status': (
                        'used' if getattr(r, 'completed_at', None)
                        else ('expired' if getattr(r, 'expires_at', None)
                                           and r.expires_at < now else 'pending')
                    ),
                })
            agg = ext.aggregate(opens=Sum('view_count'))
            token_usage_summary['meeting_links']['count'] = ext.count()
            token_usage_summary['meeting_links']['opens'] = agg.get('opens') or 0
            token_usage_summary['meeting_links']['unique_ips'] = (
                ext.exclude(last_ip__isnull=True).exclude(last_ip='')
                .values('last_ip').distinct().count()
            )
        except Exception:
            pass

        # 3. File public tokens
        try:
            from apps.files.models import FilePublicToken
            fpt = FilePublicToken.objects.filter(
                created_at__gte=since_30d,
            ).select_related('file')
            for t in fpt[:200]:
                token_link_rows.append({
                    'kind': 'File Download',
                    'subject': getattr(t.file, 'name', '') or f'File #{t.file_id}',
                    'recipient': getattr(t, 'recipient_email', '') or '',
                    'open_count': getattr(t, 'access_count', 0) or
                                  getattr(t, 'view_count', 0) or 0,
                    'last_ip': getattr(t, 'last_ip', '') or '',
                    'last_device': getattr(t, 'last_user_agent', '') or '',
                    'issued_at': getattr(t, 'created_at', None),
                    'expires_at': getattr(t, 'expires_at', None),
                    'status': (
                        'expired' if getattr(t, 'expires_at', None)
                                     and t.expires_at < now else 'active'
                    ),
                })
            token_usage_summary['file_links']['count'] = fpt.count()
            token_usage_summary['file_links']['opens'] = (
                    fpt.aggregate(opens=Sum('access_count')).get('opens') or 0
            )
            token_usage_summary['file_links']['unique_ips'] = (
                fpt.exclude(last_ip__isnull=True).exclude(last_ip='')
                .values('last_ip').distinct().count()
            )
        except Exception:
            pass

        # Sort by issued_at desc, take top 30. Push entries with no
        # issued_at to the very end so they don't break the sort.
        token_link_rows.sort(
            key=lambda r: r['issued_at'] or timezone.make_aware(
                timezone.datetime(1970, 1, 1)
            ),
            reverse=True,
        )
        ctx['token_links'] = token_link_rows[:30]
        ctx['token_summary'] = token_usage_summary
        ctx['token_total_opens'] = sum(
            v['opens'] for v in token_usage_summary.values()
        )
        ctx['token_total_links'] = sum(
            v['count'] for v in token_usage_summary.values()
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


def _humanise_duration(seconds: float) -> str:
    """
    7245.0 -> '2h 0m'.  Used for "median time-to-sign" and similar
    operational KPIs. Keeps the unit count to two so the value reads
    cleanly inside a tile (e.g. '3d 4h', '5m 12s', '<1s').
    """
    s = int(seconds or 0)
    if s < 1:
        return '<1s'
    if s < 60:
        return f'{s}s'
    if s < 3600:
        return f'{s // 60}m {s % 60}s'
    if s < 86400:
        h = s // 3600
        return f'{h}h {(s % 3600) // 60}m'
    d = s // 86400
    return f'{d}d {(s % 86400) // 3600}h'


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


# ---------------------------------------------------------------------------
# NEW: Signature Report (Supervisor / Admin)
#   E-signature usage analytics derived from apps.files.SignatureRequest,
#   SignatureRequestSigner, and (optionally) QuickSignSession.
#
# Charts:
#   • KPI strip (lifetime + last 30d + today)
#   • Status mix (donut)        — sent / partial / completed / cancelled / …
#   • Signature method (donut)  — drawn vs typed vs uploaded
#   • Daily activity (line)     — requests sent vs signatures completed
#   • Monthly trend (line)      — last 12 months
#   • Top requesters (bar)      — who's sending the most for signing
#   • Top signers (table)       — who signs the most
#   • Decline reasons (table)   — what's getting rejected and why
#   • Median time-to-sign       — operational signal
#   • Recent signature requests (table)
# ---------------------------------------------------------------------------

class SignatureReportView(_SupervisorReportsAccessMixin, TemplateView):
    template_name = 'reports/signature_report.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        from apps.files.models import SignatureRequest, SignatureRequestSigner

        now = timezone.now()
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        since_30d   = now - timedelta(days=30)
        twelve_months_ago = (now.replace(day=1) - timedelta(days=365)).replace(day=1)

        sig_qs = SignatureRequest.objects.all()

        # ── Top-line KPIs ────────────────────────────────────────────────
        # Lifetime totals
        ctx['total_requests']  = sig_qs.count()
        ctx['total_completed'] = sig_qs.filter(status='completed').count()
        ctx['total_pending']   = sig_qs.exclude(
            status__in=['completed', 'cancelled', 'expired', 'declined']
        ).count()
        ctx['total_voided']    = sig_qs.filter(
            status__in=['cancelled', 'expired']
        ).count()
        ctx['total_declined']  = sig_qs.filter(status='declined').count()

        # Quick-signed (one-click signing, no email flow)
        try:
            from apps.files.models import QuickSignSession
            ctx['total_quick_signed'] = QuickSignSession.objects.filter(
                status='completed'
            ).count()
            ctx['quick_signed_30d'] = QuickSignSession.objects.filter(
                status='completed', created_at__gte=since_30d,
            ).count()
        except Exception:
            try:
                from apps.core.models import AuditLog
                quick_actions = ['quick_sign', 'quick_signed', 'QuickSign']
                ctx['total_quick_signed'] = AuditLog.objects.filter(
                    action__in=quick_actions
                ).count()
                ctx['quick_signed_30d'] = AuditLog.objects.filter(
                    action__in=quick_actions, timestamp__gte=since_30d,
                ).count()
            except Exception:
                ctx['total_quick_signed'] = 0
                ctx['quick_signed_30d']   = 0

        # Last 30 days
        ctx['requests_30d']  = sig_qs.filter(created_at__gte=since_30d).count()
        ctx['completed_30d'] = sig_qs.filter(
            status='completed', completed_at__gte=since_30d,
        ).count()

        # Today
        ctx['requests_today'] = sig_qs.filter(created_at__gte=today_start).count()
        ctx['signed_today']   = SignatureRequestSigner.objects.filter(
            status='signed', signed_at__gte=today_start,
        ).count()

        # Completion rate (lifetime, % of requests that reached 'completed'
        # vs ones that finished any way — completed/cancelled/expired/declined)
        finished = (
            ctx['total_completed'] + ctx['total_voided'] + ctx['total_declined']
        )
        ctx['completion_rate'] = (
            round(ctx['total_completed'] / finished * 100, 1) if finished else 0
        )

        # ── Median time-to-sign (last 30d) ───────────────────────────────
        # How long, on average, does it take a signer to act once a request
        # is sent? Computed from SignatureRequestSigner.signed_at minus the
        # request's created_at. Median is more robust than mean here because
        # one stale request from 6 months ago skews the mean wildly.
        try:
            recent_signed = list(
                SignatureRequestSigner.objects.filter(
                    status='signed',
                    signed_at__gte=since_30d,
                    signed_at__isnull=False,
                ).select_related('request').values_list(
                    'request__created_at', 'signed_at',
                )
            )
            durations_sec = [
                (sa - ca).total_seconds()
                for ca, sa in recent_signed if ca and sa
            ]
            durations_sec.sort()
            if durations_sec:
                mid = len(durations_sec) // 2
                median_sec = (
                    durations_sec[mid] if len(durations_sec) % 2
                    else (durations_sec[mid - 1] + durations_sec[mid]) / 2
                )
                ctx['median_time_to_sign_human'] = _humanise_duration(median_sec)
                ctx['median_time_to_sign_sec']   = int(median_sec)
            else:
                ctx['median_time_to_sign_human'] = '—'
                ctx['median_time_to_sign_sec']   = 0
        except Exception:
            ctx['median_time_to_sign_human'] = '—'
            ctx['median_time_to_sign_sec']   = 0

        # ── Status mix (donut) ───────────────────────────────────────────
        # All-time status breakdown of every SignatureRequest. Casts label
        # via str() to flush Django lazy proxies before json.dumps.
        try:
            status_choices = {k: str(v) for k, v in SignatureRequest.Status.choices}
        except Exception:
            status_choices = {}
        status_rows = list(
            sig_qs.values('status')
            .annotate(count=Count('id'))
            .order_by('-count')
        )
        for r in status_rows:
            r['label'] = status_choices.get(r['status'], (r['status'] or 'unknown').title())
        ctx['status_mix']      = status_rows
        ctx['status_mix_json'] = json.dumps(status_rows)

        # ── Signature method mix (donut) ─────────────────────────────────
        # Drawn vs typed vs uploaded — only counts SIGNED signers (the
        # signature_type is only set once they actually sign).
        method_rows = list(
            SignatureRequestSigner.objects
            .filter(status='signed')
            .exclude(signature_type='')
            .values('signature_type')
            .annotate(count=Count('id'))
            .order_by('-count')
        )
        method_label_map = {
            'draw':   'Drawn',
            'type':   'Typed',
            'upload': 'Uploaded',
        }
        for r in method_rows:
            r['label'] = method_label_map.get(
                r['signature_type'], (r['signature_type'] or 'unknown').title()
            )
        ctx['method_mix']      = method_rows
        ctx['method_mix_json'] = json.dumps(method_rows)

        # ── Daily activity (last 30d): requests sent vs signatures done ──
        # Two parallel series the chart can plot together.
        daily_requests = {
            r['d'].isoformat(): r['count']
            for r in sig_qs.filter(created_at__gte=since_30d)
                          .annotate(d=TruncDate('created_at'))
                          .values('d')
                          .annotate(count=Count('id'))
            if r['d']
        }
        daily_signed = {
            r['d'].isoformat(): r['count']
            for r in SignatureRequestSigner.objects
                          .filter(status='signed', signed_at__gte=since_30d)
                          .annotate(d=TruncDate('signed_at'))
                          .values('d')
                          .annotate(count=Count('id'))
            if r['d']
        }
        all_days = sorted(set(daily_requests) | set(daily_signed))
        ctx['daily_series_json'] = json.dumps([
            {
                'date':     d,
                'requests': daily_requests.get(d, 0),
                'signed':   daily_signed.get(d, 0),
            }
            for d in all_days
        ])

        # ── Monthly trend (last 12 months) ───────────────────────────────
        monthly = list(
            sig_qs.filter(created_at__gte=twelve_months_ago)
            .annotate(m=TruncMonth('created_at'))
            .values('m')
            .annotate(
                count=Count('id'),
                completed=Count('id', filter=Q(status='completed')),
            )
            .order_by('m')
        )
        ctx['monthly_series_json'] = json.dumps([
            {
                'month': r['m'].strftime('%Y-%m') if r['m'] else '',
                'count':     r['count'],
                'completed': r['completed'],
            }
            for r in monthly
        ])

        # ── Top requesters (last 30d) ────────────────────────────────────
        # Who's sending the most documents for signing? Useful for spotting
        # power users + departments that should be onboarded onto templates.
        top_requesters = list(
            sig_qs.filter(created_at__gte=since_30d)
            .values(
                'created_by__email',
                'created_by__first_name',
                'created_by__last_name',
            )
            .annotate(
                count=Count('id'),
                completed=Count('id', filter=Q(status='completed')),
            )
            .order_by('-count')[:10]
        )
        ctx['top_requesters'] = top_requesters

        # ── Top signers (last 30d) ───────────────────────────────────────
        top_signers = list(
            SignatureRequestSigner.objects
            .filter(status='signed', signed_at__gte=since_30d)
            .values('email', 'name')
            .annotate(count=Count('id'))
            .order_by('-count')[:10]
        )
        ctx['top_signers'] = top_signers

        # ── Decline reasons (last 30d) ───────────────────────────────────
        # When something's getting rejected, the reason is the most useful
        # thing on the page. Empty reasons are excluded so the list reads
        # like an actual feedback queue, not a wall of blanks.
        ctx['recent_declines'] = list(
            SignatureRequestSigner.objects
            .filter(status='declined')
            .exclude(decline_reason='')
            .select_related('request', 'request__created_by')
            .order_by('-id')[:10]
        )

        # ── Recent requests (table, last 20 across all statuses) ─────────
        ctx['recent_requests'] = (
            sig_qs.select_related('created_by', 'document')
            .order_by('-created_at')[:20]
        )

        return ctx


# ---------------------------------------------------------------------------
# NEW: Orders Report (Supervisor / Admin)
#   Sales orders from phone, walk-in, website and API — counts, totals,
#   conversion rate, daily/monthly trends, source mix.
# ---------------------------------------------------------------------------

class OrdersReportView(_SupervisorReportsAccessMixin, TemplateView):
    template_name = 'reports/orders_report.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        from apps.orders.models import SalesOrder, OrderStatus, OrderSource

        now = timezone.now()
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        twelve_months_ago = (now.replace(day=1) - timedelta(days=365)).replace(day=1)
        thirty_days_ago = now - timedelta(days=30)

        # ── Top-line KPIs ────────────────────────────────────────────────
        ctx['total_orders']    = SalesOrder.objects.count()
        ctx['orders_today']    = SalesOrder.objects.filter(created_at__gte=today_start).count()
        ctx['orders_month']    = SalesOrder.objects.filter(created_at__gte=month_start).count()
        ctx['fulfilled_total'] = SalesOrder.objects.filter(status=OrderStatus.FULFILLED).count()
        ctx['fulfilled_month'] = SalesOrder.objects.filter(
            status=OrderStatus.FULFILLED, fulfilled_at__gte=month_start
        ).count()

        # Money: total billed via fulfilled orders this month
        month_qs = SalesOrder.objects.filter(
            status=OrderStatus.FULFILLED, fulfilled_at__gte=month_start
        )
        ctx['fulfilled_total_month'] = month_qs.aggregate(t=Sum('total'))['t'] or 0

        # Pending / cancelled snapshot
        _terminal_status_codes = {'fulfilled', 'cancelled', 'canceled', 'refunded', 'returned'}
        _pending_status_codes = [
            code for code, _label in OrderStatus.choices
            if code.lower() not in _terminal_status_codes
        ]
        ctx['pending_orders'] = SalesOrder.objects.filter(
            status__in=_pending_status_codes
        ).count()

        ctx['cancelled_orders']  = SalesOrder.objects.filter(status=OrderStatus.CANCELLED).count()

        # Conversion rate (fulfilled / total non-cancelled)  — last 30d
        ord30 = SalesOrder.objects.filter(created_at__gte=thirty_days_ago)
        non_cancelled_30 = ord30.exclude(status=OrderStatus.CANCELLED).count()
        fulfilled_30     = ord30.filter(status=OrderStatus.FULFILLED).count()
        ctx['conversion_rate_30d'] = (
            round(fulfilled_30 / non_cancelled_30 * 100, 1) if non_cancelled_30 else 0
        )

        # ── Source mix (donut, last 30d) ─────────────────────────────────
        source_choices = {k: str(v) for k, v in OrderSource.choices}
        sources = list(
            ord30.values('source').annotate(count=Count('id'), total=Sum('total'))
                 .order_by('-count')
        )
        for r in sources:
            r['label'] = source_choices.get(r['source'], r['source'])
            r['total'] = float(r['total'] or 0)
        ctx['source_mix']      = sources
        ctx['source_mix_json'] = json.dumps(sources)

        # ── Status mix (donut, all-time current state) ───────────────────
        status_choices = {k: str(v) for k, v in OrderStatus.choices}
        statuses = list(
            SalesOrder.objects.values('status').annotate(count=Count('id')).order_by('-count')
        )
        for r in statuses:
            r['label'] = status_choices.get(r['status'], r['status'])
        ctx['status_mix']      = statuses
        ctx['status_mix_json'] = json.dumps(statuses)

        # ── Monthly trend (last 12 months) ──────────────────────────────
        monthly = list(
            SalesOrder.objects.filter(created_at__gte=twelve_months_ago)
            .annotate(m=TruncMonth('created_at'))
            .values('m').annotate(count=Count('id'), total=Sum('total'))
            .order_by('m')
        )
        ctx['monthly_json'] = json.dumps([
            {
                'month': r['m'].strftime('%Y-%m') if r['m'] else '',
                'count': r['count'],
                'total': float(r['total'] or 0),
            }
            for r in monthly
        ])

        # ── Daily trend (last 30 days) ──────────────────────────────────
        daily = list(
            SalesOrder.objects.filter(created_at__gte=thirty_days_ago)
            .annotate(d=TruncDate('created_at'))
            .values('d').annotate(count=Count('id'), total=Sum('total'))
            .order_by('d')
        )
        ctx['daily_json'] = json.dumps([
            {
                'date': r['d'].isoformat() if r['d'] else '',
                'count': r['count'],
                'total': float(r['total'] or 0),
            }
            for r in daily
        ])

        # ── Top fulfillers ──────────────────────────────────────────────
        ctx['top_fulfillers'] = list(
            SalesOrder.objects.filter(status=OrderStatus.FULFILLED, fulfilled_by__isnull=False)
            .filter(fulfilled_at__gte=thirty_days_ago)
            .values('fulfilled_by__email', 'fulfilled_by__first_name', 'fulfilled_by__last_name')
            .annotate(count=Count('id'), total=Sum('total'))
            .order_by('-count')[:10]
        )

        # ── Recent orders ───────────────────────────────────────────────
        ctx['recent_orders'] = (
            SalesOrder.objects.select_related('customer', 'fulfilled_by')
            .order_by('-created_at')[:20]
        )

        ctx['currency'] = 'GMD'
        return ctx


# =============================================================================
# 2) NEW: CustomerServiceReportView  —  call centre + ticket analytics
# =============================================================================

class CustomerServiceReportView(_ReportsAccessMixin, TemplateView):
    """
    Customer Service operational report.

    Pulls from apps.customer_service models:
      • Customer / CustomerPhone — install base
      • CallRecord              — switchboard activity
      • ServiceTicket           — case volume + SLA
      • ServiceRating           — CSAT
      • FeedbackRequest         — feedback funnel

    Designed to answer the questions a customer-service manager
    actually walks into the office with:
        - "How many calls did we field this week?"
        - "What % of tickets are we resolving on time?"
        - "Who handled the most tickets and how fast?"
        - "What's our customer satisfaction trend?"
        - "Where is the call volume coming from?"
    """
    template_name = 'reports/customer_service_report.html'

    OPEN_TICKET_STATUSES = (
        'new', 'open', 'pending', 'assigned', 'in_progress',
        'awaiting_customer', 'escalated',
    )

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)

        now = timezone.now()
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        since_24h = now - timedelta(hours=24)
        since_7d = now - timedelta(days=7)
        since_30d = now - timedelta(days=30)
        twelve_months_ago = (now.replace(day=1) - timedelta(days=365)).replace(day=1)

        # Defaults so the template never breaks even if every model is missing
        defaults = {
            'total_customers': 0, 'new_customers_30d': 0,
            'calls_today': 0, 'calls_7d': 0, 'calls_30d': 0,
            'avg_call_duration_human': '—', 'avg_call_duration_sec': 0,
            'open_tickets': 0, 'overdue_tickets': 0, 'escalated_tickets': 0,
            'tickets_30d': 0, 'resolved_30d': 0, 'resolution_rate': 0,
            'sla_met_pct': 0, 'median_resolution_human': '—',
            'avg_csat': 0, 'csat_responses_30d': 0, 'feedback_response_rate': 0,
            'callback_pending': 0,
            # JSON blobs for charts
            'calls_by_type_json': '[]',
            'calls_hourly_json': '[]',
            'tickets_status_json': '[]',
            'tickets_priority_json': '[]',
            'daily_volume_json': '[]',
            'resolution_funnel_json': '[]',
            'csat_trend_json': '[]',
            'rating_breakdown_json': '[]',
            # Tables
            'top_agents': [], 'recent_tickets': [], 'top_complaint_customers': [],
            'department_load': [], 'recent_callbacks': [],
        }
        ctx.update(defaults)

        try:
            from apps.customer_service.models import (
                Customer, CallRecord, ServiceTicket,
                ServiceRating, FeedbackRequest,
            )
        except Exception:
            # Whole module missing — show empty page.
            return ctx

        # ── Customer base ────────────────────────────────────────────────
        ctx['total_customers'] = Customer.objects.count()
        ctx['new_customers_30d'] = Customer.objects.filter(
            created_at__gte=since_30d
        ).count()

        # ── Call volume KPIs ─────────────────────────────────────────────
        calls_qs = CallRecord.objects.all()
        ctx['calls_today'] = calls_qs.filter(started_at__gte=today_start).count()
        ctx['calls_7d'] = calls_qs.filter(started_at__gte=since_7d).count()
        ctx['calls_30d'] = calls_qs.filter(started_at__gte=since_30d).count()

        avg_dur = calls_qs.filter(
            started_at__gte=since_30d, duration_seconds__gt=0,
        ).aggregate(a=Avg('duration_seconds'))['a'] or 0
        ctx['avg_call_duration_sec'] = int(avg_dur)
        ctx['avg_call_duration_human'] = _humanise_duration(avg_dur)

        # ── Ticket KPIs ──────────────────────────────────────────────────
        ticket_qs = ServiceTicket.objects.all()
        ctx['open_tickets'] = ticket_qs.filter(
            status__in=self.OPEN_TICKET_STATUSES
        ).count()
        ctx['overdue_tickets'] = ticket_qs.filter(
            status__in=self.OPEN_TICKET_STATUSES,
            resolution_due_at__lt=now,
        ).count()
        ctx['escalated_tickets'] = ticket_qs.filter(status='escalated').count()
        ctx['tickets_30d'] = ticket_qs.filter(created_at__gte=since_30d).count()
        ctx['resolved_30d'] = ticket_qs.filter(
            resolved_at__gte=since_30d, status__in=['resolved', 'closed'],
        ).count()
        ctx['resolution_rate'] = (
            round(ctx['resolved_30d'] / ctx['tickets_30d'] * 100, 1)
            if ctx['tickets_30d'] else 0
        )

        # SLA met % — of tickets resolved in the last 30 days,
        # how many were resolved before the resolution_due_at deadline?
        resolved_with_due = ticket_qs.filter(
            resolved_at__gte=since_30d,
            resolution_due_at__isnull=False,
            status__in=['resolved', 'closed'],
        )
        sla_total = resolved_with_due.count()
        sla_met = resolved_with_due.filter(
            resolved_at__lte=F('resolution_due_at')
        ).count()
        ctx['sla_met_pct'] = round(sla_met / sla_total * 100, 1) if sla_total else 0

        # Median resolution time (last 30d) in seconds → humanised
        durs = []
        for ca, ra in resolved_with_due.values_list('created_at', 'resolved_at'):
            if ca and ra:
                durs.append((ra - ca).total_seconds())
        if durs:
            durs.sort()
            mid = len(durs) // 2
            med = durs[mid] if len(durs) % 2 else (durs[mid - 1] + durs[mid]) / 2
            ctx['median_resolution_human'] = _humanise_duration(med)
        # else stays '—'

        # Pending callbacks — tickets needing a return call
        ctx['callback_pending'] = ticket_qs.filter(
            callback_required=True, status__in=self.OPEN_TICKET_STATUSES,
        ).count()

        # ── CSAT (Customer Satisfaction) ─────────────────────────────────
        ratings_qs = ServiceRating.objects.filter(created_at__gte=since_30d)
        ctx['csat_responses_30d'] = ratings_qs.count()
        if ratings_qs.exists():
            agg = ratings_qs.aggregate(
                sq=Avg('service_quality'),
                pq=Avg('product_quality'),
                rt=Avg('response_time'),
                pr=Avg('professionalism'),
            )
            scores = [v for v in agg.values() if v is not None]
            ctx['avg_csat'] = round(sum(scores) / len(scores), 2) if scores else 0
            ctx['csat_breakdown'] = {
                'service_quality': round(agg['sq'] or 0, 2),
                'product_quality': round(agg['pq'] or 0, 2),
                'response_time': round(agg['rt'] or 0, 2),
                'professionalism': round(agg['pr'] or 0, 2),
            }
        else:
            ctx['csat_breakdown'] = {
                'service_quality': 0, 'product_quality': 0,
                'response_time': 0, 'professionalism': 0,
            }

        # Feedback request response rate (last 30d)
        try:
            fb_total = FeedbackRequest.objects.filter(
                created_at__gte=since_30d
            ).count()
            fb_completed = FeedbackRequest.objects.filter(
                created_at__gte=since_30d, completed_at__isnull=False
            ).count()
            ctx['feedback_response_rate'] = (
                round(fb_completed / fb_total * 100, 1) if fb_total else 0
            )
        except Exception:
            ctx['feedback_response_rate'] = 0

        # ── Calls by type donut (30d) ────────────────────────────────────
        type_choices = dict(CallRecord.CALL_TYPES)
        calls_by_type = list(
            calls_qs.filter(started_at__gte=since_30d)
            .values('call_type').annotate(count=Count('id')).order_by('-count')
        )
        for r in calls_by_type:
            r['label'] = type_choices.get(r['call_type'], r['call_type'])
        ctx['calls_by_type_json'] = json.dumps(calls_by_type)

        # ── Hourly call distribution heatmap data (last 7d) ──────────────
        # Pattern of when calls come in by hour-of-day. Plotted as a
        # radial / polar bar chart — visually distinctive and the right
        # shape for cyclical data (a clock face).
        hourly_counts = [0] * 24
        for c in calls_qs.filter(
                started_at__gte=since_7d
        ).values_list('started_at', flat=True):
            if c:
                hourly_counts[c.hour] += 1
        calls_hourly = [
            {'hour': h, 'label': f'{h:02d}:00', 'count': n}
            for h, n in enumerate(hourly_counts)
        ]
        ctx['calls_hourly_json'] = json.dumps(calls_hourly)
        ctx['peak_hour'] = (
            max(range(24), key=lambda h: hourly_counts[h])
            if any(hourly_counts) else None
        )
        ctx['peak_hour_count'] = max(hourly_counts) if any(hourly_counts) else 0

        # ── Ticket status mix (donut) ────────────────────────────────────
        status_choices = dict(ServiceTicket.STATUSES)
        tickets_by_status = list(
            ticket_qs.values('status')
            .annotate(count=Count('id')).order_by('-count')
        )
        for r in tickets_by_status:
            r['label'] = status_choices.get(r['status'], r['status'])
        ctx['tickets_status_json'] = json.dumps(tickets_by_status)

        # ── Ticket priority mix (donut) ──────────────────────────────────
        priority_choices = dict(ServiceTicket.PRIORITIES)
        tickets_by_priority = list(
            ticket_qs.filter(status__in=self.OPEN_TICKET_STATUSES)
            .values('priority').annotate(count=Count('id'))
            .order_by('priority')
        )
        for r in tickets_by_priority:
            r['label'] = priority_choices.get(r['priority'], r['priority'])
        ctx['tickets_priority_json'] = json.dumps(tickets_by_priority)

        # ── Daily volume — calls vs tickets vs resolved (30d) ────────────
        # A single overlaid line chart so a manager can eyeball whether
        # we're keeping up with inbound volume.
        daily_calls = {
            r['d'].isoformat(): r['c']
            for r in calls_qs.filter(started_at__gte=since_30d)
            .annotate(d=TruncDate('started_at'))
            .values('d').annotate(c=Count('id')).order_by('d')
            if r['d']
        }
        daily_tickets_in = {
            r['d'].isoformat(): r['c']
            for r in ticket_qs.filter(created_at__gte=since_30d)
            .annotate(d=TruncDate('created_at'))
            .values('d').annotate(c=Count('id')).order_by('d')
            if r['d']
        }
        daily_resolved = {
            r['d'].isoformat(): r['c']
            for r in ticket_qs.filter(
                resolved_at__gte=since_30d,
                status__in=['resolved', 'closed'],
            ).annotate(d=TruncDate('resolved_at'))
            .values('d').annotate(c=Count('id')).order_by('d')
            if r['d']
        }
        all_dates = sorted(
            set(daily_calls) | set(daily_tickets_in) | set(daily_resolved)
        )
        ctx['daily_volume_json'] = json.dumps([
            {
                'date': d,
                'calls': daily_calls.get(d, 0),
                'tickets': daily_tickets_in.get(d, 0),
                'resolved': daily_resolved.get(d, 0),
            } for d in all_dates
        ])

        # ── Resolution funnel (30d) — bar chart ──────────────────────────
        # Inbound → triaged → resolved → closed. Shows where cases drop.
        funnel = [
            {'stage': 'Created', 'count': ctx['tickets_30d']},
            {'stage': 'Assigned', 'count': ticket_qs.filter(
                created_at__gte=since_30d,
                current_owner__isnull=False,
            ).count()},
            {'stage': 'In Progress', 'count': ticket_qs.filter(
                created_at__gte=since_30d,
                status__in=['in_progress', 'assigned', 'awaiting_customer',
                            'resolved', 'closed'],
            ).count()},
            {'stage': 'Resolved', 'count': ctx['resolved_30d']},
            {'stage': 'Closed', 'count': ticket_qs.filter(
                closed_at__gte=since_30d,
            ).count()},
        ]
        ctx['resolution_funnel_json'] = json.dumps(funnel)

        # ── CSAT trend (last 12 months) ──────────────────────────────────
        csat_trend = []
        for r in (
                ServiceRating.objects.filter(created_at__gte=twelve_months_ago)
                        .annotate(m=TruncMonth('created_at'))
                        .values('m')
                        .annotate(
                    sq=Avg('service_quality'),
                    pq=Avg('product_quality'),
                    rt=Avg('response_time'),
                    pr=Avg('professionalism'),
                    n=Count('id'),
                )
                        .order_by('m')
        ):
            scores = [
                v for v in (r['sq'], r['pq'], r['rt'], r['pr'])
                if v is not None
            ]
            csat_trend.append({
                'month': r['m'].strftime('%b %Y') if r['m'] else '',
                'avg': round(sum(scores) / len(scores), 2) if scores else 0,
                'n': r['n'],
            })
        ctx['csat_trend_json'] = json.dumps(csat_trend)

        # ── Rating breakdown (radar) ─────────────────────────────────────
        rb = ctx['csat_breakdown']
        ctx['rating_breakdown_json'] = json.dumps([
            {'dim': 'Service Quality', 'score': rb['service_quality']},
            {'dim': 'Product Quality', 'score': rb['product_quality']},
            {'dim': 'Response Time', 'score': rb['response_time']},
            {'dim': 'Professionalism', 'score': rb['professionalism']},
        ])

        # ── Top agents — handler leaderboard (30d) ──────────────────────
        top_agents = list(
            ticket_qs.filter(
                resolved_at__gte=since_30d,
                resolved_by__isnull=False,
            )
            .values(
                'resolved_by__id',
                'resolved_by__email',
                'resolved_by__first_name',
                'resolved_by__last_name',
            )
            .annotate(
                resolved=Count('id'),
                avg_resolution_sec=Avg(
                    F('resolved_at') - F('created_at')
                ),
            )
            .order_by('-resolved')[:10]
        )
        # Annotate average resolution time as human-readable
        for a in top_agents:
            ar = a.get('avg_resolution_sec')
            if ar:
                # Avg of timedelta returns timedelta; convert to seconds
                try:
                    a['avg_resolution_human'] = _humanise_duration(
                        ar.total_seconds()
                    )
                except AttributeError:
                    a['avg_resolution_human'] = '—'
            else:
                a['avg_resolution_human'] = '—'
        ctx['top_agents'] = top_agents

        # ── Department load — open tickets per department ───────────────
        try:
            ctx['department_load'] = list(
                ticket_qs.filter(status__in=self.OPEN_TICKET_STATUSES)
                .exclude(department__isnull=True)
                .values('department__id', 'department__name')
                .annotate(
                    open_count=Count('id'),
                    overdue=Count('id', filter=Q(resolution_due_at__lt=now)),
                )
                .order_by('-open_count')[:10]
            )
        except Exception:
            ctx['department_load'] = []

        # ── Recent tickets table (last 15) ───────────────────────────────
        ctx['recent_tickets'] = (
            ticket_qs.select_related('customer', 'department', 'current_owner')
            .order_by('-created_at')[:15]
        )

        # ── Top customers by complaint volume (30d) ──────────────────────
        ctx['top_complaint_customers'] = list(
            ticket_qs.filter(
                created_at__gte=since_30d,
                ticket_type__in=['complaint', 'support', 'maintenance'],
            )
            .values(
                'customer__id',
                'customer__full_name',
                'customer__company_name',
            )
            .annotate(
                ticket_count=Count('id'),
                open_count=Count(
                    'id', filter=Q(status__in=self.OPEN_TICKET_STATUSES)
                ),
            )
            .order_by('-ticket_count')[:8]
        )

        # ── Recent callbacks needing attention ──────────────────────────
        ctx['recent_callbacks'] = (
            ticket_qs.filter(
                callback_required=True,
                status__in=self.OPEN_TICKET_STATUSES,
            )
            .select_related('customer', 'current_owner')
            .order_by('-created_at')[:10]
        )

        return ctx
