"""
apps/finance/views_financial_system.py
=======================================

Views for the Financial Control Center. APPEND these to your existing
apps/finance/views.py (or import from here in views.py — both work).

Includes:
  - FinancialControlCenterView   – the new dashboard
  - DashboardDataAPI             – JSON for charts (so Chart.js can refresh
                                   without a full reload when filters change)
  - IncomeStatementView          – on-screen P&L
  - CashFlowView                 – on-screen cash flow
  - BudgetVarianceView           – on-screen budget vs actual
  - AgeingReportView             – on-screen AR + AP ageing
  - AuditLogView                 – paged, filterable audit log
  - AnomalyListView / AnomalyResolveView
  - 10 export endpoints (5 reports × PDF/Excel)
"""
import json
from datetime import date
from decimal import Decimal

from django.contrib import messages
from django.http import JsonResponse, Http404
from django.shortcuts import redirect, render, get_object_or_404
from django.utils import timezone
from django.views.generic import TemplateView, View
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.paginator import Paginator
from django.db.models import Q

from apps.finance import analytics, reports
from apps.finance.models import FinanceAuditLog, FinanceAnomaly
from apps.finance.permissions import (
    FinancialSystemAccessMixin,
    can_access_financial_system,
    can_resolve_anomalies,
    is_finance, is_ceo, is_admin,
)


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _decimal(v):
    """JSON-safe conversion."""
    if isinstance(v, Decimal):
        return float(v)
    return v


def _decimals_list(lst):
    return [_decimal(x) for x in lst]


def _common_filters(request):
    """Pull period+basis from the request for views that need them."""
    period = analytics.period_from_request(request)
    basis = analytics.basis_from_request(request)
    return period, basis


# ════════════════════════════════════════════════════════════════════════════
# CONTROL CENTER (the new dashboard)
# ════════════════════════════════════════════════════════════════════════════

class FinancialControlCenterView(LoginRequiredMixin, FinancialSystemAccessMixin, TemplateView):
    """
    The new dashboard. Replaces the existing finance_dashboard for users with
    full system access (Finance/CEO/Admin). Operational users still hit the
    classic dashboard via the legacy URL.
    """
    template_name = 'finance/control_center.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        period, basis = _common_filters(self.request)
        prev = analytics.previous_period(period)

        # KPI strip
        kpi = analytics.headline_kpis(period, basis)
        prev_kpi = analytics.headline_kpis(prev, basis)
        kpi['revenue_change'] = analytics.pct_change(kpi['revenue'], prev_kpi['revenue'])
        kpi['expense_change'] = analytics.pct_change(kpi['expenses'], prev_kpi['expenses'])
        kpi['net_change']     = analytics.pct_change(kpi['net_income'], prev_kpi['net_income'])

        # Anomaly count for the alert badge
        open_anomalies = FinanceAnomaly.objects.filter(
            status=FinanceAnomaly.Status.OPEN
        ).count()
        high_anomalies = FinanceAnomaly.objects.filter(
            status=FinanceAnomaly.Status.OPEN,
            severity=FinanceAnomaly.Severity.HIGH,
        ).count()

        # Latest audit entries for the activity stream
        recent_audit = (
            FinanceAuditLog.objects
            .select_related('actor')
            .order_by('-timestamp')[:12]
        )

        # Exposure
        contract_exp = analytics.contract_exposure()
        loan_exp = analytics.loan_exposure()

        # Receivables / payables snapshot for the dashboard cards
        ar = analytics.receivables_ageing()
        ap = analytics.payables_ageing()

        # Top expense categories + budgets
        top_expenses = analytics.expense_by_type(period)[:6]
        budget_snapshot = analytics.budget_vs_actual()

        ctx.update({
            'kpi':              kpi,
            'period':           period,
            'basis':            basis,
            'period_presets':   [
                ('mtd',  'Month-to-date'),
                ('qtd',  'Quarter-to-date'),
                ('ytd',  'Year-to-date'),
                ('last_30',  'Last 30 days'),
                ('last_90',  'Last 90 days'),
                ('last_year', 'Last year'),
            ],
            'open_anomalies':   open_anomalies,
            'high_anomalies':   high_anomalies,
            'recent_audit':     recent_audit,
            'contract_exp':     contract_exp,
            'loan_exp':         loan_exp,
            'ar':               ar,
            'ap':               ap,
            'top_expenses':     top_expenses,
            'budget_snapshot':  budget_snapshot,
            'is_finance':       is_finance(self.request.user),
            'is_ceo':           is_ceo(self.request.user),
            'is_admin':         is_admin(self.request.user),
        })
        return ctx


# ─── JSON API for charts (so filter changes don't reload the page) ──────────

class DashboardDataAPI(LoginRequiredMixin, FinancialSystemAccessMixin, View):
    """
    Returns all chart-ready series as JSON. Called by the dashboard
    when the user changes period/basis.
    """
    def get(self, request):
        period, basis = _common_filters(request)
        kpi = analytics.headline_kpis(period, basis)

        rev_exp = analytics.revenue_vs_expense_series(period, basis)
        cash = analytics.cash_flow_daily(period) if period.days <= 120 else None
        by_type = analytics.expense_by_type(period)
        by_dept = analytics.expense_by_department(period)
        by_customer = analytics.revenue_by_customer(period, basis, limit=8)

        return JsonResponse({
            'period': {
                'start': period.start.isoformat(),
                'end':   period.end.isoformat(),
                'label': period.label,
                'days':  period.days,
            },
            'basis': basis,
            'kpi': {
                'revenue':      _decimal(kpi['revenue']),
                'expenses':     _decimal(kpi['expenses']),
                'net_income':   _decimal(kpi['net_income']),
                'margin_pct':   _decimal(kpi['margin_pct']),
                'daily_burn':   _decimal(kpi['daily_burn']),
                'cash_on_hand': _decimal(kpi['cash_on_hand']),
                'runway_days':  _decimal(kpi['runway_days']) if kpi['runway_days'] is not None else None,
            },
            'revenue_vs_expense': {
                'labels':   rev_exp['labels'],
                'revenue':  _decimals_list(rev_exp['revenue']),
                'expenses': _decimals_list(rev_exp['expenses']),
                'net':      _decimals_list(rev_exp['net']),
            },
            'cash_flow_daily': {
                'labels':     cash['labels'] if cash else [],
                'cash_in':    _decimals_list(cash['cash_in']) if cash else [],
                'cash_out':   _decimals_list(cash['cash_out']) if cash else [],
                'cumulative': _decimals_list(cash['cumulative']) if cash else [],
            } if cash else None,
            'expense_by_type': [
                {'label': r['label'], 'amount': _decimal(r['amount'])}
                for r in by_type
            ],
            'expense_by_department': [
                {'department': r['department'], 'amount': _decimal(r['amount'])}
                for r in by_dept
            ],
            'revenue_by_customer': [
                {'customer': r['customer'], 'amount': _decimal(r['amount'])}
                for r in by_customer
            ],
        })


# ════════════════════════════════════════════════════════════════════════════
# REPORT VIEWS (on-screen, with export buttons)
# ════════════════════════════════════════════════════════════════════════════

class IncomeStatementView(LoginRequiredMixin, FinancialSystemAccessMixin, TemplateView):
    template_name = 'finance/reports/income_statement.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        period, basis = _common_filters(self.request)
        ctx['data'] = analytics.income_statement(period, basis)
        ctx['period'] = period
        ctx['basis'] = basis
        return ctx


class CashFlowView(LoginRequiredMixin, FinancialSystemAccessMixin, TemplateView):
    template_name = 'finance/reports/cash_flow.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        period, _ = _common_filters(self.request)
        ctx['data'] = analytics.cash_flow_statement(period)
        ctx['period'] = period
        return ctx


class BudgetVarianceView(LoginRequiredMixin, FinancialSystemAccessMixin, TemplateView):
    template_name = 'finance/reports/budget_variance.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        fy = self.request.GET.get('fy')
        try:
            fy = int(fy) if fy else None
        except ValueError:
            fy = None
        ctx['data'] = analytics.budget_vs_actual(fy)
        return ctx


class AgeingReportView(LoginRequiredMixin, FinancialSystemAccessMixin, TemplateView):
    template_name = 'finance/reports/ageing.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['ar'] = analytics.receivables_ageing()
        ctx['ap'] = analytics.payables_ageing()
        return ctx


# ════════════════════════════════════════════════════════════════════════════
# AUDIT LOG
# ════════════════════════════════════════════════════════════════════════════

class AuditLogView(LoginRequiredMixin, FinancialSystemAccessMixin, TemplateView):
    template_name = 'finance/audit_log.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        request = self.request

        qs = FinanceAuditLog.objects.select_related('actor').order_by('-timestamp')

        # Filters
        actor_id = request.GET.get('actor')
        action = request.GET.get('action')
        model = request.GET.get('model')
        q = request.GET.get('q', '').strip()
        date_from = request.GET.get('from')
        date_to = request.GET.get('to')

        if actor_id:
            qs = qs.filter(actor_id=actor_id)
        if action:
            qs = qs.filter(action=action)
        if model:
            qs = qs.filter(model_name=model)
        if q:
            qs = qs.filter(
                Q(object_repr__icontains=q) |
                Q(actor_name__icontains=q) |
                Q(notes__icontains=q)
            )
        if date_from:
            try:
                qs = qs.filter(timestamp__date__gte=date.fromisoformat(date_from))
            except ValueError:
                pass
        if date_to:
            try:
                qs = qs.filter(timestamp__date__lte=date.fromisoformat(date_to))
            except ValueError:
                pass

        paginator = Paginator(qs, 50)
        page = paginator.get_page(request.GET.get('page'))

        ctx.update({
            'page':         page,
            'total_count':  paginator.count,
            'action_choices': FinanceAuditLog.Action.choices,
            'model_choices': sorted(set(
                FinanceAuditLog.objects.values_list('model_name', flat=True).distinct()
            )),
            'filters': {
                'actor':  actor_id or '',
                'action': action or '',
                'model':  model or '',
                'q':      q,
                'from':   date_from or '',
                'to':     date_to or '',
            },
        })
        return ctx


class AuditEntryDetailView(LoginRequiredMixin, FinancialSystemAccessMixin, TemplateView):
    template_name = 'finance/audit_entry.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['entry'] = get_object_or_404(FinanceAuditLog, pk=kwargs['pk'])
        return ctx


# ════════════════════════════════════════════════════════════════════════════
# ANOMALIES
# ════════════════════════════════════════════════════════════════════════════

class AnomalyListView(LoginRequiredMixin, FinancialSystemAccessMixin, TemplateView):
    template_name = 'finance/anomalies.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        status = self.request.GET.get('status', 'open')
        severity = self.request.GET.get('severity', '')

        qs = FinanceAnomaly.objects.select_related('reviewed_by').order_by('-detected_at')
        if status and status != 'all':
            qs = qs.filter(status=status)
        if severity:
            qs = qs.filter(severity=severity)

        paginator = Paginator(qs, 30)
        ctx['page'] = paginator.get_page(self.request.GET.get('page'))
        ctx['filters'] = {'status': status, 'severity': severity}
        ctx['counts'] = {
            'open':      FinanceAnomaly.objects.filter(status=FinanceAnomaly.Status.OPEN).count(),
            'high':      FinanceAnomaly.objects.filter(
                            status=FinanceAnomaly.Status.OPEN,
                            severity=FinanceAnomaly.Severity.HIGH).count(),
            'reviewing': FinanceAnomaly.objects.filter(status=FinanceAnomaly.Status.REVIEWING).count(),
            'resolved':  FinanceAnomaly.objects.filter(status=FinanceAnomaly.Status.RESOLVED).count(),
        }
        ctx['can_resolve'] = can_resolve_anomalies(self.request.user)
        return ctx


class AnomalyResolveView(LoginRequiredMixin, FinancialSystemAccessMixin, View):
    def post(self, request, pk):
        if not can_resolve_anomalies(request.user):
            messages.error(request, 'Only CEO and Admin staff may resolve anomalies.')
            return redirect('finance_anomaly_list')

        anomaly = get_object_or_404(FinanceAnomaly, pk=pk)
        new_status = request.POST.get('status', 'resolved')
        if new_status not in dict(FinanceAnomaly.Status.choices):
            new_status = FinanceAnomaly.Status.RESOLVED

        anomaly.status = new_status
        anomaly.resolution = request.POST.get('resolution', '').strip()
        anomaly.reviewed_by = request.user
        anomaly.reviewed_at = timezone.now()
        anomaly.save(update_fields=['status', 'resolution', 'reviewed_by', 'reviewed_at'])

        messages.success(request, f'Anomaly marked {anomaly.get_status_display()}.')
        return redirect('finance_anomaly_list')


# ════════════════════════════════════════════════════════════════════════════
# EXPORTS — 5 reports × {PDF, XLSX} = 10 endpoints
# ════════════════════════════════════════════════════════════════════════════

def _log_export(user, request, kind, fmt):
    """Drop a FinanceAuditLog row for every export."""
    try:
        FinanceAuditLog.objects.create(
            actor=user,
            actor_name=(user.get_full_name() or user.username) if user else 'System',
            ip_address=request.META.get('REMOTE_ADDR'),
            action=FinanceAuditLog.Action.EXPORT,
            model_name='Report',
            object_repr=f'{kind} ({fmt.upper()})',
            notes=f'Exported {kind} as {fmt.upper()} for {request.GET.urlencode() or "default period"}',
        )
    except Exception:
        pass


class IncomeStatementExportPDF(LoginRequiredMixin, FinancialSystemAccessMixin, View):
    def get(self, request):
        period, basis = _common_filters(request)
        _log_export(request.user, request, 'Income Statement', 'pdf')
        return reports.export_pl_pdf(period, basis)


class IncomeStatementExportXLSX(LoginRequiredMixin, FinancialSystemAccessMixin, View):
    def get(self, request):
        period, basis = _common_filters(request)
        _log_export(request.user, request, 'Income Statement', 'xlsx')
        return reports.export_pl_xlsx(period, basis)


class CashFlowExportPDF(LoginRequiredMixin, FinancialSystemAccessMixin, View):
    def get(self, request):
        period, _ = _common_filters(request)
        _log_export(request.user, request, 'Cash Flow', 'pdf')
        return reports.export_cashflow_pdf(period)


class CashFlowExportXLSX(LoginRequiredMixin, FinancialSystemAccessMixin, View):
    def get(self, request):
        period, _ = _common_filters(request)
        _log_export(request.user, request, 'Cash Flow', 'xlsx')
        return reports.export_cashflow_xlsx(period)


class BudgetVarianceExportPDF(LoginRequiredMixin, FinancialSystemAccessMixin, View):
    def get(self, request):
        fy = request.GET.get('fy')
        try:
            fy = int(fy) if fy else None
        except ValueError:
            fy = None
        _log_export(request.user, request, 'Budget vs Actual', 'pdf')
        return reports.export_budget_vs_actual_pdf(fy)


class BudgetVarianceExportXLSX(LoginRequiredMixin, FinancialSystemAccessMixin, View):
    def get(self, request):
        fy = request.GET.get('fy')
        try:
            fy = int(fy) if fy else None
        except ValueError:
            fy = None
        _log_export(request.user, request, 'Budget vs Actual', 'xlsx')
        return reports.export_budget_vs_actual_xlsx(fy)


class AuditTrailExportPDF(LoginRequiredMixin, FinancialSystemAccessMixin, View):
    def get(self, request):
        period, _ = _common_filters(request)
        _log_export(request.user, request, 'Audit Trail', 'pdf')
        return reports.export_audit_pdf(period)


class AuditTrailExportXLSX(LoginRequiredMixin, FinancialSystemAccessMixin, View):
    def get(self, request):
        period, _ = _common_filters(request)
        _log_export(request.user, request, 'Audit Trail', 'xlsx')
        return reports.export_audit_xlsx(period)


class AgeingExportPDF(LoginRequiredMixin, FinancialSystemAccessMixin, View):
    def get(self, request):
        _log_export(request.user, request, 'Ageing', 'pdf')
        return reports.export_ageing_pdf()


class AgeingExportXLSX(LoginRequiredMixin, FinancialSystemAccessMixin, View):
    def get(self, request):
        _log_export(request.user, request, 'Ageing', 'xlsx')
        return reports.export_ageing_xlsx()
