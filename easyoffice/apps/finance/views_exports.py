"""
apps/finance/views_exports.py
=============================

Download endpoints for the finance list screens. Each view rebuilds the SAME
filtered queryset as its list view (so the export honours the user's current
search / status / method / year filters) and streams a CSV or XLSX file.

Format is chosen with ?format=csv or ?format=xlsx (default xlsx).

Permissions mirror the list views:
    * Invoices & Payment Requests → _can_manage_payment_requests
    * Payments                    → finance/CEO see all; others see only their
                                    own (same rule as PaymentListView)
"""
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import Q
from django.shortcuts import redirect
from django.utils import timezone
from django.views.generic import View

from apps.finance.models import (
    IncomingPaymentRequest, PaymentRequest, Payment,
)
from apps.finance.views import (
    _can_manage_payment_requests, _is_finance, _is_ceo,
)
from apps.finance import list_exports


def _fmt(request):
    fmt = (request.GET.get('format') or 'xlsx').lower()
    return 'csv' if fmt == 'csv' else 'xlsx'


# ── Invoices (receivables) ───────────────────────────────────────────────────

class InvoiceExportView(LoginRequiredMixin, View):
    """GET /finance/invoices/export/?format=csv|xlsx&q=&status="""

    def dispatch(self, request, *args, **kwargs):
        if not _can_manage_payment_requests(request.user):
            messages.error(request, 'You do not have permission to export invoices.')
            return redirect('finance_dashboard')
        return super().dispatch(request, *args, **kwargs)

    def get(self, request):
        q = request.GET.get('q', '').strip()
        status = request.GET.get('status', '').strip()

        qs = IncomingPaymentRequest.objects.select_related(
            'created_by', 'project', 'budget'
        ).order_by('-created_at')

        if q:
            qs = qs.filter(
                Q(invoice_number__icontains=q)
                | Q(customer_name__icontains=q)
                | Q(customer_email__icontains=q)
                | Q(customer_company__icontains=q)
                | Q(title__icontains=q)
            )
        if status:
            qs = qs.filter(status=status)

        return list_exports.export_invoices(qs, _fmt(request))


# ── Payment Requests (payables) ──────────────────────────────────────────────

class PaymentRequestExportView(LoginRequiredMixin, View):
    """GET /finance/payment-requests/export/?format=csv|xlsx&q=&status=&rtype="""

    def dispatch(self, request, *args, **kwargs):
        if not _can_manage_payment_requests(request.user):
            messages.error(request, 'You do not have permission to export payment requests.')
            return redirect('finance_dashboard')
        return super().dispatch(request, *args, **kwargs)

    def get(self, request):
        q = request.GET.get('q', '').strip()
        status = request.GET.get('status', '').strip()
        rtype = request.GET.get('rtype', '').strip()

        qs = PaymentRequest.objects.select_related(
            'requested_by', 'recipient_user', 'budget', 'project', 'linked_payment'
        ).order_by('-created_at')

        if q:
            qs = qs.filter(
                Q(title__icontains=q)
                | Q(recipient_name__icontains=q)
                | Q(recipient_email__icontains=q)
                | Q(description__icontains=q)
            )
        if status:
            qs = qs.filter(status=status)
        if rtype:
            qs = qs.filter(recipient_type=rtype)

        return list_exports.export_payment_requests(qs, _fmt(request))


# ── Payments (disbursements) ─────────────────────────────────────────────────

class PaymentExportView(LoginRequiredMixin, View):
    """GET /finance/payments/export/?format=csv|xlsx&q=&method=&year="""

    def get(self, request):
        q = request.GET.get('q', '').strip()
        method = request.GET.get('method', '').strip()
        year = request.GET.get('year', '').strip()

        qs = Payment.objects.select_related(
            'paid_by', 'budget', 'purchase_request', 'employee', 'project', 'employee_request'
        )

        # Same visibility rule as PaymentListView: non-finance users only see
        # payments they are party to.
        if not (_is_finance(request.user) or _is_ceo(request.user)):
            qs = qs.filter(Q(employee=request.user) | Q(paid_by=request.user))

        if q:
            qs = qs.filter(
                Q(reference__icontains=q)
                | Q(description__icontains=q)
                | Q(recipient__icontains=q)
            )
        if method:
            qs = qs.filter(method=method)
        if year:
            qs = qs.filter(payment_date__year=year)

        qs = qs.order_by('-payment_date', '-created_at')

        return list_exports.export_payments(qs, _fmt(request))
