"""
apps/finance/views_financial_system.py
=======================================

Views for the Financial Control Center.

Includes:
  - FinancialControlCenterView   – the new dashboard
  - DashboardDataAPI             – JSON for charts (so Chart.js can refresh
                                   without a full reload when filters change)
  - IncomeStatementView          – on-screen P&L
  - CashFlowView                 – on-screen cash flow
  - BudgetVarianceView           – on-screen budget vs actual
  - AgeingReportView             – on-screen AR + AP ageing
  - AuditLogView / AuditEntryDetailView
  - AnomalyListView / AnomalyResolveView
  - FinanceInvoiceListView       – NEW: paid/unpaid/overdue invoice list
  - FinanceInvoiceMarkPaidView   – NEW: marks an invoice paid + creates ledger entry
  - 10 export endpoints (5 reports × PDF/Excel)
"""
import json
import logging
from datetime import date as _date
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.paginator import Paginator
from django.db.models import Q, Sum, Count
from django.http import JsonResponse, Http404, HttpResponseRedirect
from django.shortcuts import redirect, render, get_object_or_404
from django.urls import reverse
from django.utils import timezone
from django.views import View
from django.views.generic import TemplateView

from apps.finance import analytics, reports
from apps.finance.models import FinanceAuditLog, FinanceAnomaly
from apps.finance.permissions import (
    FinancialSystemAccessMixin,
    can_access_financial_system,
    can_resolve_anomalies,
    is_finance, is_ceo, is_admin,
)


log = logging.getLogger(__name__)


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

        # Invoice counts for the Open Invoices card
        invoice_counts = self._get_invoice_counts()

        # Top expense categories + budgets
        top_expenses = analytics.expense_by_type(period)[:6]
        budget_snapshot = analytics.budget_vs_actual()

        # POS takings for the period (own card + already folded into revenue).
        from apps.finance import pos_revenue as _pos
        pos_summary = _pos.pos_summary(period)

        ctx.update({
            'pos_summary':      pos_summary,
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
            'invoice_counts':   invoice_counts,
            'top_expenses':     top_expenses,
            'budget_snapshot':  budget_snapshot,
            'is_finance':       is_finance(self.request.user),
            'is_ceo':           is_ceo(self.request.user),
            'is_admin':         is_admin(self.request.user),
        })
        return ctx

    def _get_invoice_counts(self):
        """
        Counts of finalized billable invoices by payment state, plus
        outstanding value. Mirrors the logic used by FinanceInvoiceListView
        so the dashboard numbers match the invoices page exactly.
        """
        from apps.invoices.models import InvoiceDocument, DocType
        today = timezone.localdate()

        base = InvoiceDocument.objects.filter(
            doc_type=DocType.INVOICE,
            status=InvoiceDocument.Status.FINALIZED,
        )
        unpaid = base.filter(paid_at__isnull=True)
        overdue = unpaid.filter(due_date__isnull=False, due_date__lt=today)

        return {
            'total':     base.count(),
            'paid':      base.filter(paid_at__isnull=False).count(),
            'unpaid':    unpaid.count(),
            'overdue':   overdue.count(),
            'unpaid_amount':  unpaid.aggregate(s=Sum('total'))['s'] or Decimal('0'),
            'overdue_amount': overdue.aggregate(s=Sum('total'))['s'] or Decimal('0'),
        }


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
        from apps.finance import pos_revenue as _pos
        pos_summary = _pos.pos_summary(period)

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
            'pos': {
                'available': pos_summary['available'],
                'total': str(pos_summary['total']),
                'count': pos_summary['count'],
                'by_method': [
                    {'method': m['method'], 'amount': str(m['amount']),
                     'count': m['count']}
                    for m in pos_summary['by_method']
                ],
            },
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
                qs = qs.filter(timestamp__date__gte=_date.fromisoformat(date_from))
            except ValueError:
                pass
        if date_to:
            try:
                qs = qs.filter(timestamp__date__lte=_date.fromisoformat(date_to))
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
# INVOICES — finance-side list with mark-as-paid action
# ════════════════════════════════════════════════════════════════════════════

class FinanceInvoiceListView(LoginRequiredMixin, FinancialSystemAccessMixin, TemplateView):
    """
    Finance-only invoice listing inside the Control Center. Shows every
    finalized billable InvoiceDocument with payment status, plus a quick
    inline 'Mark as Paid' action.

    Filters:
        ?status   = paid | unpaid | overdue | all     (default: unpaid)
        ?q        = free text — matches client_name, number, client_email
        ?from     = invoice_date >=    (YYYY-MM-DD)
        ?to       = invoice_date <=    (YYYY-MM-DD)
        ?currency = ISO code
    """
    template_name = 'finance/invoices_list.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)

        # Lazy import to keep finance ↔ invoices coupling at the runtime layer.
        from apps.invoices.models import InvoiceDocument, DocType

        request = self.request
        status   = (request.GET.get('status') or 'unpaid').strip().lower()
        q        = (request.GET.get('q') or '').strip()
        date_from = request.GET.get('from')
        date_to   = request.GET.get('to')
        currency = (request.GET.get('currency') or '').strip().upper()

        # Base set: only billable finalized invoices. Drafts have no number,
        # voided invoices are dead, proformas/delivery notes aren't billable.
        qs = (
            InvoiceDocument.objects
            .filter(doc_type=DocType.INVOICE,
                    status=InvoiceDocument.Status.FINALIZED)
            .select_related('linked_receivable', 'created_by',
                            'paid_by', 'linked_payment')
            .order_by('-invoice_date', '-created_at')
        )

        today = timezone.localdate()

        if status == 'paid':
            qs = qs.filter(paid_at__isnull=False)
        elif status == 'unpaid':
            qs = qs.filter(paid_at__isnull=True)
        elif status == 'overdue':
            qs = qs.filter(
                paid_at__isnull=True,
                due_date__isnull=False,
                due_date__lt=today,
            )
        # 'all' → no filter

        if q:
            qs = qs.filter(
                Q(client_name__icontains=q)
                | Q(number__icontains=q)
                | Q(client_email__icontains=q)
                | Q(po_reference__icontains=q)
            )
        if date_from:
            try:
                qs = qs.filter(invoice_date__gte=_date.fromisoformat(date_from))
            except ValueError:
                pass
        if date_to:
            try:
                qs = qs.filter(invoice_date__lte=_date.fromisoformat(date_to))
            except ValueError:
                pass
        if currency:
            qs = qs.filter(currency=currency)

        # Headline numbers across the *filtered* queryset
        agg = qs.aggregate(
            total_count       = Count('id'),
            total_amount      = Sum('total'),
            paid_count        = Count('id', filter=Q(paid_at__isnull=False)),
            paid_amount       = Sum('total', filter=Q(paid_at__isnull=False)),
            unpaid_amount     = Sum('total', filter=Q(paid_at__isnull=True)),
            overdue_amount    = Sum('total', filter=Q(
                paid_at__isnull=True,
                due_date__isnull=False,
                due_date__lt=today,
            )),
        )

        # Counts for the tab pills (unfiltered base — so totals don't move
        # when the user clicks between tabs)
        base = InvoiceDocument.objects.filter(
            doc_type=DocType.INVOICE,
            status=InvoiceDocument.Status.FINALIZED,
        )
        tab_counts = {
            'all':     base.count(),
            'paid':    base.filter(paid_at__isnull=False).count(),
            'unpaid':  base.filter(paid_at__isnull=True).count(),
            'overdue': base.filter(
                paid_at__isnull=True,
                due_date__isnull=False,
                due_date__lt=today,
            ).count(),
        }

        # Pre-build the tab list with counts so the template doesn't need
        # a custom dict-lookup filter.
        status_tabs = [
            {'key': 'unpaid',  'label': 'Unpaid',  'count': tab_counts['unpaid']},
            {'key': 'overdue', 'label': 'Overdue', 'count': tab_counts['overdue']},
            {'key': 'paid',    'label': 'Paid',    'count': tab_counts['paid']},
            {'key': 'all',     'label': 'All',     'count': tab_counts['all']},
        ]

        # Available currencies for the filter dropdown
        currencies = sorted(set(
            base.values_list('currency', flat=True).distinct()
        ))

        paginator = Paginator(qs, 25)
        page = paginator.get_page(request.GET.get('page'))

        ctx.update({
            'page':        page,
            'agg':         agg,
            'tab_counts':  tab_counts,
            'status_tabs': status_tabs,
            'currencies':  currencies,
            'today':       today,
            'filters': {
                'status':   status,
                'q':        q,
                'from':     date_from or '',
                'to':       date_to or '',
                'currency': currency,
            },
            'payment_method_choices': [
                ('bank_transfer', 'Bank transfer'),
                ('cash',          'Cash'),
                ('cheque',        'Cheque'),
                ('mobile_money',  'Mobile money'),
                ('card',          'Card'),
                ('other',         'Other'),
            ],
        })
        return ctx


class FinanceInvoiceMarkPaidView(LoginRequiredMixin, FinancialSystemAccessMixin, View):
    """
    POST endpoint — finance staff marks an InvoiceDocument as paid.
    Form fields: amount, method, reference, paid_on.
    All optional except amount (defaults to invoice.total).
    """
    http_method_names = ['post']

    def post(self, request, pk):
        from apps.invoices.models import InvoiceDocument
        from apps.invoices.services import mark_invoice_paid

        invoice = get_object_or_404(InvoiceDocument, pk=pk)

        if not invoice.can_be_marked_paid:
            messages.error(
                request,
                f'Invoice {invoice.number} cannot be marked paid in its current state '
                f'({invoice.payment_status_label}).',
            )
            return redirect(self._next_url(request))

        # Parse fields
        raw_amount = (request.POST.get('amount') or '').strip()
        amount = None
        if raw_amount:
            try:
                amount = Decimal(raw_amount)
            except InvalidOperation:
                messages.error(request, 'Invalid amount entered.')
                return redirect(self._next_url(request))

        method = (request.POST.get('method') or 'bank_transfer').strip()
        reference = (request.POST.get('reference') or '').strip()
        raw_paid_on = (request.POST.get('paid_on') or '').strip()
        paid_on = None
        if raw_paid_on:
            try:
                paid_on = _date.fromisoformat(raw_paid_on)
            except ValueError:
                messages.error(request, 'Invalid payment date — use YYYY-MM-DD.')
                return redirect(self._next_url(request))

        try:
            mark_invoice_paid(
                invoice, request.user,
                amount=amount,
                method=method,
                reference=reference,
                paid_on=paid_on,
            )
        except ValueError as exc:
            messages.error(request, str(exc))
            return redirect(self._next_url(request))
        except Exception as exc:
            # Log the full traceback for ops, AND surface the exception text
            # to the user so they can act on it without shell access.
            log.exception('Failed to mark invoice %s paid', invoice.pk)
            messages.error(
                request,
                f'Could not mark the invoice paid: '
                f'{type(exc).__name__}: {exc}',
            )
            return redirect(self._next_url(request))

        messages.success(
            request,
            f'Invoice {invoice.number} marked paid. '
            f'A payment ledger entry has been created.',
        )
        return redirect(self._next_url(request))

    def _next_url(self, request):
        nxt = request.POST.get('next') or request.META.get('HTTP_REFERER')
        if nxt:
            return nxt
        return reverse('finance_invoice_list')


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