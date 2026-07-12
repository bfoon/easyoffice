import os
from decimal import Decimal, InvalidOperation
from django.contrib.auth.mixins import LoginRequiredMixin
from django.views.generic import TemplateView, View, DetailView
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.utils import timezone
from django.db.models import Sum, Q, Count
from django.http import HttpResponseForbidden
from django.core.mail import send_mail
from django.conf import settings
from apps.core.models import User
from django.urls import reverse
from apps.tasks.models import Task
from apps.finance.views_invoice_followup import _can_assign_followup

from apps.finance.models import (
    Budget, PurchaseRequest, Payment,
    EmployeeFinanceRequest, EmployeeLoan, EmployeeLoanPayment,
    Contract, ContractAlertLog, ContractExtension, ContractInvoiceLink, IncomingPaymentRequest,
    ContractDocument, ContractSignatureRequest, ContractSignature,
    PaymentRequest, PaymentRequestDocument,
    IncomingPaymentDocument,
)

from apps.invoices.models import InvoiceTemplate
from apps.invoices.permissions import can_use_invoices
from .contract_invoice_service import (
    generate_invoice_for_contract,
    ContractInvoiceError,
)


# ── Permission helpers ───────────────────────────────────────────────────────

def _is_finance(user):
    return user.is_superuser or user.groups.filter(name__in=['Finance', 'Admin']).exists()


def _is_ceo(user):
    if user.is_superuser:
        return True
    if user.groups.filter(name__in=['CEO']).exists():
        return True
    profile = getattr(user, 'staffprofile', None)
    title = ''
    try:
        title = (
            getattr(profile.position, 'title', '')
            if profile and getattr(profile, 'position', None) else ''
        )
    except Exception:
        title = ''
    return str(title).strip().lower() == 'ceo'


def _is_admin(user):
    return user.is_superuser or user.groups.filter(name__in=['Admin']).exists()


def _is_hr(user):
    return user.is_superuser or user.groups.filter(name__in=['HR', 'Human Resources']).exists()


def _can_view_contracts(user):
    return _is_finance(user) or _is_hr(user) or _is_ceo(user) or _is_admin(user)


def _can_manage_contracts(user):
    return _is_finance(user) or _is_ceo(user) or _is_admin(user)


def _can_view_purchase(user, purchase):
    if _is_finance(user) or _is_ceo(user):
        return True
    return purchase.requested_by_id == user.id


def _can_edit_purchase(user, purchase):
    if _is_finance(user):
        return True
    if purchase.requested_by_id != user.id:
        return False
    return purchase.status in [
        PurchaseRequest.Status.DRAFT,
        PurchaseRequest.Status.REJECTED,
    ]


def _can_view_finance_dashboard(user):
    return _is_finance(user) or _is_ceo(user) or _is_hr(user)


def _can_view_budgets(user):
    if not user or not getattr(user, 'is_authenticated', False):
        return False
    return _is_finance(user) or _is_ceo(user)


def _can_view_my_finance(user):
    return user.is_authenticated


def _can_manage_payment_requests(user):
    return _is_finance(user) or _is_ceo(user) or user.is_superuser


def _user_can_manage_contract_invoices(user):
    """
    Superuser OR (Finance/CEO group member who also has invoice-app access).
    """
    if not user.is_authenticated:
        return False
    if user.is_superuser:
        return True
    in_finance_or_ceo = user.groups.filter(name__iregex=r'^(Finance|CEO)$').exists()
    return in_finance_or_ceo and can_use_invoices(user)


def _user_template_qs(user):
    """Templates this user may use: shared ones + their own."""
    if user.is_superuser:
        return InvoiceTemplate.objects.all()
    return InvoiceTemplate.objects.filter(
        Q(visibility=InvoiceTemplate.Visibility.SHARED) | Q(owner=user)
    )


# ── Misc helpers ─────────────────────────────────────────────────────────────

def _validate_decimal(value, field_name='amount'):
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError(f'Please enter a valid {field_name}.')


def _generate_reference(prefix='PAY'):
    return f'{prefix}-{timezone.now().strftime("%Y%m%d%H%M%S%f")}'


def _parse_date(value, field_name='date'):
    """
    Coerce a form value into a real datetime.date (or None for empty).

    Required because Django's `auto-cast on save` only runs at the SQL
    layer — `post_save` signals fire with whatever Python value is on the
    instance, so if `c.end_date = '2026-12-31'` (a string) is assigned and
    a signal then does `instance.end_date + timedelta(days=1)`, it blows
    up with: 'can only concatenate str (not timedelta) to str'.
    """
    if value in (None, ''):
        return None
    # Already a date / datetime — pass through (datetime → date).
    if hasattr(value, 'date') and not isinstance(value, str):
        return value.date() if hasattr(value, 'time') else value
    s = str(value).strip()
    if not s:
        return None
    from datetime import datetime as _dt
    for fmt in ('%Y-%m-%d', '%d/%m/%Y', '%m/%d/%Y'):
        try:
            return _dt.strptime(s, fmt).date()
        except ValueError:
            continue
    raise ValueError(f'Please enter a valid {field_name} (YYYY-MM-DD).')


def _apply_payment_to_budget(payment):
    """Deduct a payment from its linked budget exactly once."""
    budget = getattr(payment, 'budget', None)
    amount = getattr(payment, 'amount', None)
    if not budget or amount is None or amount <= 0:
        return
    new_spent = (budget.spent_amount or Decimal('0')) + amount
    if new_spent > budget.total_amount:
        raise ValueError(f'Payment exceeds available budget for "{budget.name}".')
    budget.spent_amount = new_spent
    budget.save(update_fields=['spent_amount'])


def _next_contract_reference():
    return f'CTR-{timezone.now().strftime("%Y%m%d%H%M%S%f")}'


def _send_contract_email(subject, message, recipients):
    recipients = [r for r in recipients if r]
    if not recipients:
        return
    try:
        send_mail(
            subject=subject,
            message=message,
            from_email=getattr(settings, 'DEFAULT_FROM_EMAIL', 'noreply@example.com'),
            recipient_list=recipients,
            fail_silently=True,
        )
    except Exception:
        pass


def _contract_role_recipients():
    return User.objects.filter(
        is_active=True
    ).filter(
        Q(groups__name__in=['Finance', 'HR', 'CEO', 'Admin']) | Q(is_superuser=True)
    ).distinct()


# ── Finance Dashboard ────────────────────────────────────────────────────────

class FinanceDashboardView(LoginRequiredMixin, TemplateView):
    template_name = 'finance/dashboard.html'

    def dispatch(self, request, *args, **kwargs):
        if not _can_view_finance_dashboard(request.user):
            messages.error(request, 'You do not have permission to view the Finance Dashboard.')
            return redirect('my_finance_dashboard')
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)

        user = self.request.user
        today = timezone.now()
        year = today.year
        month = today.month
        today_date = today.date()

        active_budgets = Budget.objects.filter(fiscal_year=year, status=Budget.Status.ACTIVE)

        total_budget = active_budgets.aggregate(t=Sum('total_amount'))['t'] or Decimal('0')
        total_spent = active_budgets.aggregate(t=Sum('spent_amount'))['t'] or Decimal('0')
        total_balance = total_budget - total_spent

        my_requests = PurchaseRequest.objects.filter(requested_by=user)

        payments_this_month = Payment.objects.filter(
            payment_date__year=year,
            payment_date__month=month,
        ).aggregate(t=Sum('amount'))['t'] or Decimal('0')

        budgets = active_budgets.select_related('department', 'unit').order_by('-created_at')[:8]

        pending_purchases = PurchaseRequest.objects.filter(
            status=PurchaseRequest.Status.SUBMITTED
        ).select_related('requested_by', 'department', 'budget', 'project').order_by('-created_at')[:10]

        pending_staff_requests = EmployeeFinanceRequest.objects.filter(
            status=EmployeeFinanceRequest.Status.SUBMITTED
        ).select_related('employee', 'budget', 'project').order_by('-created_at')[:10]

        finance_processing_queue = EmployeeFinanceRequest.objects.filter(
            status=EmployeeFinanceRequest.Status.CEO_APPROVED
        ).select_related('employee', 'budget', 'project').order_by('-approval_date', '-created_at')[:10]

        recent_payments = Payment.objects.select_related(
            'paid_by', 'purchase_request', 'budget', 'employee', 'project'
        ).order_by('-payment_date', '-created_at')[:8]

        recent_payment_requests = PaymentRequest.objects.select_related(
            'requested_by', 'recipient_user',
        ).order_by('-created_at')[:5]

        # Contract monitoring
        contract_qs = Contract.objects.select_related(
            'staff', 'project', 'budget', 'created_by'
        ).order_by('end_date', '-created_at')

        expiring_contracts = []
        expired_contracts = []
        due_invoice_contracts = []

        for c in contract_qs:
            try:
                c.update_status_by_dates()
            except Exception:
                pass
            try:
                if c.days_to_end >= 0 and c.days_to_end <= (c.alert_days_before_end or 30):
                    expiring_contracts.append(c)
            except Exception:
                pass
            try:
                if c.is_expired:
                    expired_contracts.append(c)
            except Exception:
                pass
            try:
                if (
                    c.auto_generate_invoice
                    and c.next_invoice_date
                    and c.next_invoice_date <= today_date
                    and c.status not in [Contract.Status.EXPIRED, Contract.Status.TERMINATED]
                ):
                    due_invoice_contracts.append(c)
            except Exception:
                pass

        recent_incoming_invoices = IncomingPaymentRequest.objects.select_related(
            'project', 'budget', 'created_by'
        ).order_by('-created_at')[:5]

        ctx.update({
            'total_budget': total_budget,
            'total_spent': total_spent,
            'total_balance': total_balance,
            'overall_pct': round(float(total_spent) / float(total_budget) * 100, 1) if total_budget else 0,
            'pending_count': PurchaseRequest.objects.filter(status=PurchaseRequest.Status.SUBMITTED).count(),
            'approved_count': PurchaseRequest.objects.filter(status=PurchaseRequest.Status.CEO_APPROVED).count(),
            'payments_this_month': payments_this_month,
            'budgets': budgets,
            'pending_purchases': pending_purchases,
            'pending_staff_requests': pending_staff_requests,
            'finance_processing_queue': finance_processing_queue,
            'recent_payments': recent_payments,
            'recent_payment_requests': recent_payment_requests,
            'recent_incoming_invoices': recent_incoming_invoices,
            'my_recent_requests': my_requests.select_related(
                'department', 'budget', 'project'
            ).order_by('-created_at')[:6],
            'my_pending_requests': my_requests.filter(status=PurchaseRequest.Status.SUBMITTED).count(),
            'my_draft_requests': my_requests.filter(status=PurchaseRequest.Status.DRAFT).count(),
            'contract_total_count': contract_qs.count(),
            'contract_staff_count': contract_qs.filter(contract_type=Contract.ContractType.STAFF).count(),
            'contract_vendor_count': contract_qs.filter(contract_type=Contract.ContractType.VENDOR).count(),
            'contract_expiring_count': len(expiring_contracts),
            'contract_expired_count': len(expired_contracts),
            'contract_due_invoice_count': len(due_invoice_contracts),
            'expiring_contracts': expiring_contracts[:6],
            'expired_contracts': expired_contracts[:6],
            'due_invoice_contracts': due_invoice_contracts[:6],
            'is_finance': _is_finance(user),
            'is_ceo': _is_ceo(user),
            'is_hr': _is_hr(user),
            'can_view_finance_dashboard': _can_view_finance_dashboard(user),
            'can_view_my_finance': _can_view_my_finance(user),
        })
        return ctx


# ── My Finance Dashboard ─────────────────────────────────────────────────────

class MyFinanceDashboardView(LoginRequiredMixin, TemplateView):
    template_name = 'finance/my_finance_dashboard.html'

    def dispatch(self, request, *args, **kwargs):
        if not _can_view_my_finance(request.user):
            messages.error(request, 'You do not have permission to view My Finance.')
            return redirect('login')
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        user = self.request.user
        year = timezone.now().year
        month = timezone.now().month

        my_payments = Payment.objects.filter(employee=user).select_related(
            'budget', 'project', 'purchase_request', 'employee_request'
        ).order_by('-payment_date', '-created_at')

        my_loans = EmployeeLoan.objects.filter(employee=user).order_by('-created_at')
        active_loan = my_loans.filter(status='active').first()

        ctx.update({
            'my_payments': my_payments[:20],
            'my_requests': EmployeeFinanceRequest.objects.filter(
                employee=user
            ).order_by('-created_at')[:12],
            'my_loans': my_loans,
            'active_loan': active_loan,
            'paid_this_month': my_payments.filter(
                direction=Payment.Direction.COMPANY_TO_STAFF,
                payment_date__year=year,
                payment_date__month=month,
            ).aggregate(t=Sum('amount'))['t'] or Decimal('0'),
            'paid_ytd': my_payments.filter(
                direction=Payment.Direction.COMPANY_TO_STAFF,
                payment_date__year=year,
            ).aggregate(t=Sum('amount'))['t'] or Decimal('0'),
            'deductions_ytd': my_payments.filter(
                direction=Payment.Direction.STAFF_TO_COMPANY,
                payment_date__year=year,
            ).aggregate(t=Sum('amount'))['t'] or Decimal('0'),
            'salary_ytd': my_payments.filter(
                payment_type=Payment.PaymentType.SALARY,
                payment_date__year=year,
            ).aggregate(t=Sum('amount'))['t'] or Decimal('0'),
            'allowance_ytd': my_payments.filter(
                payment_type=Payment.PaymentType.ALLOWANCE,
                payment_date__year=year,
            ).aggregate(t=Sum('amount'))['t'] or Decimal('0'),
            'overtime_ytd': my_payments.filter(
                payment_type=Payment.PaymentType.OVERTIME,
                payment_date__year=year,
            ).aggregate(t=Sum('amount'))['t'] or Decimal('0'),
            'transport_ytd': my_payments.filter(
                payment_type=Payment.PaymentType.TRANSPORT_REFUND,
                payment_date__year=year,
            ).aggregate(t=Sum('amount'))['t'] or Decimal('0'),
            'is_finance': _is_finance(user),
            'is_ceo': _is_ceo(user),
            'is_hr': _is_hr(user),
            'can_view_finance_dashboard': _can_view_finance_dashboard(user),
            'can_view_my_finance': _can_view_my_finance(user),
        })
        return ctx


# ── Budget List ──────────────────────────────────────────────────────────────

class BudgetListView(LoginRequiredMixin, TemplateView):
    template_name = 'finance/budget_list.html'

    def dispatch(self, request, *args, **kwargs):
        if not _can_view_budgets(request.user):
            return HttpResponseForbidden('You do not have permission to view budgets.')
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        year = int(self.request.GET.get('year', timezone.now().year))
        status = self.request.GET.get('status', '').strip()

        qs = Budget.objects.filter(fiscal_year=year).select_related('department', 'unit', 'approved_by')
        if status:
            qs = qs.filter(status=status)

        totals = qs.aggregate(
            total_amount=Sum('total_amount'),
            spent_amount=Sum('spent_amount'),
            allocated_amount=Sum('allocated_amount'),
        )

        ctx.update({
            'budgets': qs.order_by('name'),
            'year': year,
            'status_filter': status,
            'status_choices': Budget.Status.choices,
            'year_range': range(timezone.now().year - 2, timezone.now().year + 3),
            'grand_total': totals['total_amount'] or Decimal('0'),
            'grand_spent': totals['spent_amount'] or Decimal('0'),
            'grand_allocated': totals['allocated_amount'] or Decimal('0'),
            'is_finance': _is_finance(self.request.user),
            'is_ceo': _is_ceo(self.request.user),
        })
        return ctx


# ── Purchase Requests ────────────────────────────────────────────────────────

class PurchaseRequestView(LoginRequiredMixin, View):
    template_name = 'finance/purchase_form.html'

    def _ctx(self, request, edit_obj=None):
        from apps.organization.models import Department
        try:
            from apps.projects.models import Project
            projects = Project.objects.filter(
                Q(team_members=request.user) | Q(project_manager=request.user)
            ).distinct().order_by('name')
        except Exception:
            projects = []

        if _is_finance(request.user) or _is_ceo(request.user):
            visible_requests = PurchaseRequest.objects.select_related(
                'requested_by', 'department', 'budget', 'project', 'approved_by', 'processed_by'
            ).order_by('-created_at')[:50]
        else:
            visible_requests = PurchaseRequest.objects.filter(
                requested_by=request.user
            ).select_related(
                'department', 'budget', 'project', 'approved_by', 'processed_by'
            ).order_by('-created_at')[:25]

        return {
            'departments': Department.objects.filter(is_active=True),
            'budgets': Budget.objects.filter(status='active').order_by('-fiscal_year', 'name'),
            'projects': projects,
            'purchase_requests': visible_requests,
            'priority_choices': PurchaseRequest.Priority.choices,
            'status_choices': PurchaseRequest.Status.choices,
            'edit_obj': edit_obj,
            'is_finance': _is_finance(request.user),
            'is_ceo': _is_ceo(request.user),
        }

    def get(self, request):
        edit_id = request.GET.get('edit')
        edit_obj = None
        if edit_id:
            candidate = get_object_or_404(PurchaseRequest, id=edit_id)
            if _can_edit_purchase(request.user, candidate):
                edit_obj = candidate
        return render(request, self.template_name, self._ctx(request, edit_obj=edit_obj))

    def post(self, request):
        cancel_id = request.POST.get('cancel_id')
        if cancel_id:
            pr = get_object_or_404(
                PurchaseRequest,
                id=cancel_id,
                requested_by=request.user,
                status=PurchaseRequest.Status.DRAFT,
            )
            pr.status = PurchaseRequest.Status.CLOSED
            pr.save(update_fields=['status', 'updated_at'])
            messages.success(request, 'Request cancelled.')
            return redirect('purchase_request')

        request_id = request.POST.get('request_id')
        save_as = request.POST.get('save_as', 'submitted')

        try:
            estimated = _validate_decimal(request.POST.get('estimated_cost'), 'estimated cost')
        except ValueError as e:
            messages.error(request, str(e))
            return render(request, self.template_name, self._ctx(request))

        title = request.POST.get('title', '').strip()
        description = request.POST.get('description', '').strip()
        justification = request.POST.get('justification', '').strip()

        if not title or not description or not justification:
            messages.error(request, 'Title, description and justification are required.')
            return render(request, self.template_name, self._ctx(request))

        status_value = (
            PurchaseRequest.Status.DRAFT
            if save_as == 'draft'
            else PurchaseRequest.Status.SUBMITTED
        )

        if request_id:
            pr = get_object_or_404(PurchaseRequest, id=request_id)
            if not _can_edit_purchase(request.user, pr):
                return HttpResponseForbidden('You do not have permission to edit this request.')
            pr.title = title
            pr.description = description
            pr.department_id = request.POST.get('department') or None
            pr.budget_id = request.POST.get('budget') or None
            pr.project_id = request.POST.get('project') or None
            pr.estimated_cost = estimated
            pr.priority = request.POST.get('priority', PurchaseRequest.Priority.NORMAL)
            pr.justification = justification
            pr.vendor = request.POST.get('vendor', '').strip()
            pr.expected_delivery = request.POST.get('expected_delivery') or None
            pr.status = status_value
            if 'attachment' in request.FILES:
                pr.attachment = request.FILES['attachment']
            pr.save()
            messages.success(request, f'"{pr.title}" updated successfully.')
            return redirect('purchase_request')

        pr = PurchaseRequest.objects.create(
            title=title,
            description=description,
            requested_by=request.user,
            department_id=request.POST.get('department') or None,
            budget_id=request.POST.get('budget') or None,
            project_id=request.POST.get('project') or None,
            estimated_cost=estimated,
            priority=request.POST.get('priority', PurchaseRequest.Priority.NORMAL),
            justification=justification,
            vendor=request.POST.get('vendor', '').strip(),
            expected_delivery=request.POST.get('expected_delivery') or None,
            status=status_value,
        )
        if 'attachment' in request.FILES:
            pr.attachment = request.FILES['attachment']
            pr.save(update_fields=['attachment'])

        messages.success(
            request,
            f'Draft "{pr.title}" saved.'
            if status_value == PurchaseRequest.Status.DRAFT
            else f'Purchase request "{pr.title}" submitted for CEO approval.',
        )
        return redirect('purchase_request')


class PurchaseRequestDetailView(LoginRequiredMixin, TemplateView):
    template_name = 'finance/purchase_detail.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        purchase = get_object_or_404(PurchaseRequest, pk=self.kwargs['pk'])
        ctx.update({
            'purchase': purchase,
            'payments': Payment.objects.filter(purchase_request=purchase).order_by('-payment_date'),
            'status_choices': PurchaseRequest.Status.choices,
            'is_ceo': _is_ceo(self.request.user),
            'is_finance': _is_finance(self.request.user),
            'can_edit_purchase': (
                purchase.requested_by == self.request.user
                and purchase.status in ['draft', 'rejected']
            ),
        })
        return ctx


class PurchaseApprovalView(LoginRequiredMixin, View):
    def post(self, request, pk):
        pr = get_object_or_404(PurchaseRequest, pk=pk)
        action = request.POST.get('action')
        next_url = request.POST.get('next', '')

        if action in ['approve', 'reject'] and not _is_ceo(request.user):
            messages.error(request, 'Only the CEO can approve or reject purchase requests.')
            return redirect(next_url if next_url.startswith('/') else 'finance_dashboard')

        if action in ['mark_processing', 'update_status'] and not _is_finance(request.user):
            messages.error(request, 'Only finance can process purchase requests.')
            return redirect(next_url if next_url.startswith('/') else 'finance_dashboard')

        if action == 'approve':
            pr.status = PurchaseRequest.Status.CEO_APPROVED
            pr.approved_by = request.user
            pr.approval_date = timezone.now()
            pr.approval_notes = request.POST.get('notes', '')
            pr.save()
            messages.success(request, f'"{pr.title}" approved by CEO.')

        elif action == 'reject':
            pr.status = PurchaseRequest.Status.REJECTED
            pr.approved_by = request.user
            pr.approval_date = timezone.now()
            pr.approval_notes = request.POST.get('notes', '')
            pr.save()
            messages.warning(request, f'"{pr.title}" rejected.')

        elif action == 'mark_processing':
            pr.status = PurchaseRequest.Status.PROCESSING
            pr.processed_by = request.user
            pr.processed_at = timezone.now()
            pr.notes = request.POST.get('notes', pr.notes)
            pr.save(update_fields=['status', 'processed_by', 'processed_at', 'notes', 'updated_at'])
            messages.success(request, 'Purchase request moved to processing.')

        elif action == 'update_status':
            new_status = request.POST.get('status', '')
            if new_status in dict(PurchaseRequest.Status.choices):
                pr.status = new_status
                pr.processed_by = request.user
                pr.processed_at = timezone.now()
                pr.notes = request.POST.get('notes', pr.notes)
                pr.save(update_fields=['status', 'processed_by', 'processed_at', 'notes', 'updated_at'])
                messages.success(request, 'Purchase status updated.')

        return redirect(next_url if next_url.startswith('/') else 'finance_dashboard')


# ── Staff Finance Requests ───────────────────────────────────────────────────

class EmployeeFinanceRequestView(LoginRequiredMixin, View):
    template_name = 'finance/employee_request_form.html'

    def _ctx(self, request):
        from apps.projects.models import Project
        return {
            'request_types': EmployeeFinanceRequest.RequestType.choices,
            'budgets': Budget.objects.filter(status='active').order_by('-fiscal_year', 'name'),
            'projects': Project.objects.filter(
                Q(team_members=request.user) | Q(project_manager=request.user)
            ).distinct().order_by('name'),
            'my_requests': EmployeeFinanceRequest.objects.filter(
                employee=request.user
            ).select_related(
                'budget', 'project', 'approved_by', 'processed_by', 'linked_payment'
            ).order_by('-created_at')[:25],
        }

    def get(self, request):
        return render(request, self.template_name, self._ctx(request))

    def post(self, request):
        action = request.POST.get('form_action', 'submit')

        if action == 'cancel':
            req = get_object_or_404(
                EmployeeFinanceRequest,
                id=request.POST.get('request_id'),
                employee=request.user,
                status=EmployeeFinanceRequest.Status.DRAFT,
            )
            req.status = EmployeeFinanceRequest.Status.CLOSED
            req.save(update_fields=['status', 'updated_at'])
            messages.success(request, 'Request cancelled.')
            return redirect('employee_finance_request')

        try:
            amount = _validate_decimal(request.POST.get('amount_requested'), 'requested amount')
        except ValueError as e:
            messages.error(request, str(e))
            return render(request, self.template_name, self._ctx(request))

        status = (
            EmployeeFinanceRequest.Status.DRAFT
            if action == 'draft'
            else EmployeeFinanceRequest.Status.SUBMITTED
        )

        req = EmployeeFinanceRequest.objects.create(
            employee=request.user,
            request_type=request.POST.get('request_type'),
            title=request.POST.get('title', '').strip(),
            reason=request.POST.get('reason', '').strip(),
            amount_requested=amount,
            status=status,
            quantity=request.POST.get('quantity') or None,
            unit_label=request.POST.get('unit_label', '').strip(),
            project_id=request.POST.get('project') or None,
            budget_id=request.POST.get('budget') or None,
        )
        if 'attachment' in request.FILES:
            req.attachment = request.FILES['attachment']
            req.save(update_fields=['attachment'])

        messages.success(
            request,
            f'Draft "{req.title}" saved.'
            if status == EmployeeFinanceRequest.Status.DRAFT
            else f'"{req.title}" submitted for CEO approval.',
        )
        return redirect('employee_finance_request')


class EmployeeFinanceRequestActionView(LoginRequiredMixin, View):
    def post(self, request, pk):
        req = get_object_or_404(EmployeeFinanceRequest, pk=pk)
        action = request.POST.get('action')
        next_url = request.POST.get('next', '')

        if action in ['approve', 'reject'] and not _is_ceo(request.user):
            messages.error(request, 'Only the CEO can approve or reject staff finance requests.')
            return redirect(next_url if next_url.startswith('/') else 'finance_dashboard')

        if action == 'approve':
            req.status = EmployeeFinanceRequest.Status.CEO_APPROVED
            req.amount_approved = request.POST.get('amount_approved') or req.amount_requested
            req.approved_by = request.user
            req.approval_date = timezone.now()
            req.approval_notes = request.POST.get('notes', '')
            req.save()
            messages.success(request, f'"{req.title}" approved by CEO.')

        elif action == 'reject':
            req.status = EmployeeFinanceRequest.Status.REJECTED
            req.approved_by = request.user
            req.approval_date = timezone.now()
            req.approval_notes = request.POST.get('notes', '')
            req.save()
            messages.warning(request, f'"{req.title}" rejected.')

        return redirect(next_url if next_url.startswith('/') else 'finance_dashboard')


class FinanceRequestQueueView(LoginRequiredMixin, TemplateView):
    template_name = 'finance/finance_request_queue.html'

    def dispatch(self, request, *args, **kwargs):
        if not (_is_finance(request.user) or _is_hr(request.user)):
            return HttpResponseForbidden('You do not have permission to process finance requests.')
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx.update({
            'to_process': EmployeeFinanceRequest.objects.filter(
                status='ceo_approved'
            ).select_related(
                'employee', 'budget', 'project', 'approved_by'
            ).order_by('-approval_date', '-created_at'),
            'processed': EmployeeFinanceRequest.objects.filter(
                status__in=['processing', 'paid', 'closed']
            ).select_related(
                'employee', 'budget', 'project', 'processed_by', 'linked_payment'
            ).order_by('-processed_at', '-created_at')[:30],
        })
        return ctx


class FinanceRequestProcessView(LoginRequiredMixin, View):
    def post(self, request, pk):
        if not _is_finance(request.user):
            messages.error(request, 'Permission denied.')
            return redirect('finance_dashboard')

        req = get_object_or_404(EmployeeFinanceRequest, pk=pk)

        if req.status != EmployeeFinanceRequest.Status.CEO_APPROVED:
            messages.error(request, 'This request is not ready for finance processing.')
            return redirect('finance_request_queue')

        try:
            amount = _validate_decimal(request.POST.get('amount'), 'payment amount')
        except ValueError as e:
            messages.error(request, str(e))
            return redirect('finance_request_queue')

        payment_type_map = {
            EmployeeFinanceRequest.RequestType.LOAN: Payment.PaymentType.LOAN_DISBURSEMENT,
            EmployeeFinanceRequest.RequestType.OVERTIME: Payment.PaymentType.OVERTIME,
            EmployeeFinanceRequest.RequestType.TRANSPORT_REFUND: Payment.PaymentType.TRANSPORT_REFUND,
            EmployeeFinanceRequest.RequestType.LEAVE_SELL_BACK: Payment.PaymentType.LEAVE_SELL_BACK,
            EmployeeFinanceRequest.RequestType.SALARY_ADVANCE: Payment.PaymentType.SALARY_ADVANCE,
            EmployeeFinanceRequest.RequestType.ALLOWANCE: Payment.PaymentType.ALLOWANCE,
            EmployeeFinanceRequest.RequestType.REIMBURSEMENT: Payment.PaymentType.REIMBURSEMENT,
            EmployeeFinanceRequest.RequestType.OTHER: Payment.PaymentType.OTHER,
        }

        payment = Payment.objects.create(
            reference=_generate_reference('PAY'),
            description=req.title,
            employee_request=req,
            amount=amount,
            currency=request.POST.get('currency', 'USD'),
            method=request.POST.get('method', Payment.Method.BANK_TRANSFER),
            direction=Payment.Direction.COMPANY_TO_STAFF,
            payment_type=payment_type_map.get(req.request_type, Payment.PaymentType.OTHER),
            paid_by=request.user,
            processed_by=request.user,
            approved_by=req.approved_by,
            employee=req.employee,
            recipient=(
                req.employee.get_full_name()
                if hasattr(req.employee, 'get_full_name')
                else str(req.employee)
            ),
            payment_date=request.POST.get('payment_date') or timezone.now().date(),
            budget=req.budget,
            project=req.project,
            notes=request.POST.get('notes', ''),
        )

        if 'receipt' in request.FILES:
            payment.receipt = request.FILES['receipt']
            payment.save(update_fields=['receipt'])

        _apply_payment_to_budget(payment)

        req.status = EmployeeFinanceRequest.Status.PAID
        req.processed_by = request.user
        req.processed_at = timezone.now()
        req.linked_payment = payment
        req.save(update_fields=['status', 'processed_by', 'processed_at', 'linked_payment', 'updated_at'])

        if req.request_type == EmployeeFinanceRequest.RequestType.LOAN:
            approved_amount = req.amount_approved or req.amount_requested
            repayment_months = int(request.POST.get('repayment_months') or 12)
            monthly_payment = approved_amount / repayment_months if repayment_months else approved_amount
            EmployeeLoan.objects.create(
                employee=req.employee,
                source_request=req,
                principal_amount=req.amount_requested,
                approved_amount=approved_amount,
                disbursed_amount=amount,
                repayment_months=repayment_months,
                monthly_payment=monthly_payment,
                approved_by=req.approved_by,
            )

        messages.success(request, f'"{req.title}" processed and payment recorded.')
        return redirect('finance_request_queue')


# ── My Loans ────────────────────────────────────────────────────────────────

class MyLoanListView(LoginRequiredMixin, TemplateView):
    template_name = 'finance/my_loans.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        loans = EmployeeLoan.objects.filter(
            employee=self.request.user
        ).prefetch_related('repayment_rows', 'payments').order_by('-created_at')
        ctx.update({'loans': loans})
        return ctx


# ── Payments ────────────────────────────────────────────────────────────────

class PaymentListView(LoginRequiredMixin, TemplateView):
    template_name = 'finance/payment_list.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)

        q = self.request.GET.get('q', '').strip()
        method = self.request.GET.get('method', '').strip()
        year = self.request.GET.get('year', '').strip()

        qs = Payment.objects.select_related(
            'paid_by', 'budget', 'purchase_request', 'employee', 'project', 'employee_request'
        )

        if not (_is_finance(self.request.user) or _is_ceo(self.request.user)):
            qs = qs.filter(Q(employee=self.request.user) | Q(paid_by=self.request.user))

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

        qs = qs.order_by('-payment_date', '-created_at')[:100]
        total_paid = qs.aggregate(t=Sum('amount'))['t'] or Decimal('0')

        ctx.update({
            'payments': qs,
            'q': q,
            'method_filter': method,
            'year_filter': year,
            'method_choices': Payment.Method.choices,
            'year_range': range(timezone.now().year - 2, timezone.now().year + 1),
            'total_paid': total_paid,
            'is_finance': _is_finance(self.request.user),
            'is_ceo': _is_ceo(self.request.user),
        })
        return ctx


# ── Payment Request email helpers ────────────────────────────────────────────

def _send_payment_request_email(payment_request, request=None):
    """Send a professional notification email to the recipient."""
    from django.core.mail import EmailMessage

    recipient_email = payment_request.effective_recipient_email
    if not recipient_email:
        return

    office_name = getattr(settings, 'OFFICE_NAME', 'EasyOffice')
    org_name    = getattr(settings, 'ORGANISATION_NAME', office_name)
    currency    = payment_request.currency
    amount      = f'{payment_request.amount:,.2f}'
    pay_method  = payment_request.get_method_display()
    due_date    = (
        payment_request.due_date.strftime('%d %B %Y')
        if payment_request.due_date else 'As soon as possible'
    )
    requester = getattr(payment_request.requested_by, 'full_name', str(payment_request.requested_by))

    description_block = (
        f'<div style="background:#fffbeb;border:1px solid #fde68a;border-radius:10px;'
        f'padding:14px 18px;font-size:14px;color:#92400e;margin:20px 0">'
        f'<strong>Description:</strong><br>{payment_request.description}</div>'
        if payment_request.description else ''
    )
    notes_block = (
        f'<div style="background:#f0fdf4;border:1px solid #86efac;border-radius:10px;'
        f'padding:14px 18px;font-size:14px;color:#14532d;margin:20px 0">'
        f'<strong>Payment Details / Bank Information:</strong><br>{payment_request.recipient_notes}</div>'
        if payment_request.recipient_notes else ''
    )

    html = f"""<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"/>
<style>
  body{{margin:0;padding:0;background:#f1f5f9;font-family:'Segoe UI',Arial,sans-serif;}}
  .wrapper{{max-width:620px;margin:32px auto;background:#fff;border-radius:16px;overflow:hidden;box-shadow:0 4px 24px rgba(0,0,0,.08);}}
  .header{{background:linear-gradient(135deg,#1e3a5f 0%,#2563eb 100%);padding:36px 40px 32px;text-align:center;}}
  .header h1{{margin:0;font-size:22px;color:#fff;font-weight:700;}}
  .header p{{margin:6px 0 0;font-size:13px;color:rgba(255,255,255,.75);}}
  .badge{{display:inline-block;background:rgba(255,255,255,.2);color:#fff;border-radius:20px;padding:4px 14px;font-size:12px;font-weight:700;margin-top:12px;}}
  .body{{padding:36px 40px;}}
  .amount-box{{background:linear-gradient(135deg,#eff6ff,#dbeafe);border:1px solid #bfdbfe;border-radius:12px;padding:20px 24px;text-align:center;margin:24px 0;}}
  .amount-box .label{{font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:.06em;color:#1d4ed8;margin-bottom:6px;}}
  .amount-box .value{{font-size:32px;font-weight:800;color:#1e3a5f;}}
  .detail-table{{width:100%;border-collapse:collapse;margin:24px 0;}}
  .detail-table td{{padding:11px 14px;font-size:14px;}}
  .detail-table tr:nth-child(odd) td{{background:#f8fafc;}}
  .detail-table td:first-child{{font-weight:700;color:#475569;width:38%;border-radius:6px 0 0 6px;}}
  .detail-table td:last-child{{color:#1e293b;border-radius:0 6px 6px 0;}}
  .footer{{background:#f8fafc;padding:24px 40px;text-align:center;border-top:1px solid #e2e8f0;}}
  .footer p{{margin:0;font-size:12px;color:#94a3b8;line-height:1.7;}}
</style>
</head>
<body>
<div class="wrapper">
  <div class="header">
    <h1>💳 Payment Notification</h1>
    <p>{org_name} — Finance Department</p>
    <span class="badge">{payment_request.get_payment_type_display()}</span>
  </div>
  <div class="body">
    <p style="font-size:16px;color:#1e293b;line-height:1.6">
      Dear <strong>{payment_request.effective_recipient_name}</strong>,<br><br>
      A payment has been initiated on your behalf by the Finance Department of <strong>{org_name}</strong>.
    </p>
    <div class="amount-box">
      <div class="label">Payment Amount</div>
      <div class="value">{currency} {amount}</div>
    </div>
    <table class="detail-table">
      <tr><td>Reference</td><td>{payment_request.id}</td></tr>
      <tr><td>Title</td><td>{payment_request.title}</td></tr>
      <tr><td>Payment Type</td><td>{payment_request.get_payment_type_display()}</td></tr>
      <tr><td>Payment Method</td><td>{pay_method}</td></tr>
      <tr><td>Expected By</td><td>{due_date}</td></tr>
      <tr><td>Authorised By</td><td>{requester}</td></tr>
    </table>
    {description_block}
    {notes_block}
    <p style="font-size:13px;color:#64748b;line-height:1.7;margin-top:20px">
      If you have any questions, contact the Finance Department directly.
      Please do not reply to this automated email.
    </p>
  </div>
  <div class="footer">
    <p><strong>{org_name}</strong> · Finance &amp; Administration<br>
    This is an automated notification. Please retain it for your records.</p>
  </div>
</div>
</body>
</html>"""

    msg = EmailMessage(
        subject=f'Payment Notification — {payment_request.title} | {org_name}',
        body=html,
        from_email=getattr(
            settings, 'DEFAULT_FROM_EMAIL',
            f'finance@{org_name.lower().replace(" ", "")}.org',
        ),
        to=[recipient_email],
    )
    msg.content_subtype = 'html'
    try:
        msg.send()
    except Exception:
        pass


def _send_payment_confirmation_email(payment_request):
    """Send a payment confirmation email after a request is marked paid."""
    from django.core.mail import EmailMessage

    recipient_email = payment_request.effective_recipient_email
    if not recipient_email:
        return

    office_name = getattr(settings, 'OFFICE_NAME', 'EasyOffice')
    org_name    = getattr(settings, 'ORGANISATION_NAME', office_name)
    currency    = payment_request.currency
    amount      = f'{payment_request.amount:,.2f}'
    pay_method  = payment_request.get_method_display()
    paid_at     = (
        payment_request.paid_at.strftime('%d %B %Y at %H:%M')
        if payment_request.paid_at else 'Today'
    )
    ref = (
        payment_request.linked_payment.reference
        if payment_request.linked_payment
        else str(payment_request.id)
    )

    html = f"""<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"/>
<style>
  body{{margin:0;padding:0;background:#f1f5f9;font-family:'Segoe UI',Arial,sans-serif;}}
  .wrapper{{max-width:620px;margin:32px auto;background:#fff;border-radius:16px;overflow:hidden;box-shadow:0 4px 24px rgba(0,0,0,.08);}}
  .header{{background:linear-gradient(135deg,#064e3b 0%,#10b981 100%);padding:36px 40px 32px;text-align:center;}}
  .header .checkmark{{font-size:48px;margin-bottom:8px;display:block;}}
  .header h1{{margin:0;font-size:22px;color:#fff;font-weight:700;}}
  .header p{{margin:6px 0 0;font-size:13px;color:rgba(255,255,255,.75);}}
  .body{{padding:36px 40px;}}
  .amount-box{{background:linear-gradient(135deg,#ecfdf5,#d1fae5);border:1px solid #6ee7b7;border-radius:12px;padding:20px 24px;text-align:center;margin:24px 0;}}
  .amount-box .label{{font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:.06em;color:#065f46;margin-bottom:6px;}}
  .amount-box .value{{font-size:32px;font-weight:800;color:#064e3b;}}
  .detail-table{{width:100%;border-collapse:collapse;margin:24px 0;}}
  .detail-table td{{padding:11px 14px;font-size:14px;}}
  .detail-table tr:nth-child(odd) td{{background:#f8fafc;}}
  .detail-table td:first-child{{font-weight:700;color:#475569;width:38%;border-radius:6px 0 0 6px;}}
  .detail-table td:last-child{{color:#1e293b;border-radius:0 6px 6px 0;}}
  .success-bar{{background:#d1fae5;border-radius:10px;padding:14px 18px;text-align:center;font-size:14px;font-weight:700;color:#065f46;margin:20px 0;border:1px solid #6ee7b7;}}
  .footer{{background:#f8fafc;padding:24px 40px;text-align:center;border-top:1px solid #e2e8f0;}}
  .footer p{{margin:0;font-size:12px;color:#94a3b8;line-height:1.7;}}
</style>
</head>
<body>
<div class="wrapper">
  <div class="header">
    <span class="checkmark">✅</span>
    <h1>Payment Confirmed</h1>
    <p>{org_name} — Finance Department</p>
  </div>
  <div class="body">
    <p style="font-size:16px;color:#1e293b;line-height:1.6">
      Dear <strong>{payment_request.effective_recipient_name}</strong>,<br><br>
      The following payment has been <strong>successfully processed</strong> by {org_name}.
    </p>
    <div class="amount-box">
      <div class="label">Amount Paid</div>
      <div class="value">{currency} {amount}</div>
    </div>
    <div class="success-bar">✓ Payment Successfully Processed on {paid_at}</div>
    <table class="detail-table">
      <tr><td>Payment Reference</td><td><strong>{ref}</strong></td></tr>
      <tr><td>Title</td><td>{payment_request.title}</td></tr>
      <tr><td>Payment Type</td><td>{payment_request.get_payment_type_display()}</td></tr>
      <tr><td>Payment Method</td><td>{pay_method}</td></tr>
      <tr><td>Date Processed</td><td>{paid_at}</td></tr>
    </table>
    <p style="font-size:13px;color:#64748b;line-height:1.7;margin-top:16px">
      If you believe this payment was made in error, contact the Finance Department
      immediately quoting the reference above. Please do not reply to this automated email.
    </p>
  </div>
  <div class="footer">
    <p><strong>{org_name}</strong> · Finance &amp; Administration<br>
    This is an automated payment confirmation. Please retain it for your records.</p>
  </div>
</div>
</body>
</html>"""

    msg = EmailMessage(
        subject=f'Payment Confirmation — {ref} | {org_name}',
        body=html,
        from_email=getattr(
            settings, 'DEFAULT_FROM_EMAIL',
            f'finance@{org_name.lower().replace(" ", "")}.org',
        ),
        to=[recipient_email],
    )
    msg.content_subtype = 'html'
    try:
        msg.send()
    except Exception:
        pass


# ── Payment Request Views ────────────────────────────────────────────────────

class PaymentRequestListView(LoginRequiredMixin, TemplateView):
    template_name = 'finance/payment_request_list.html'

    def dispatch(self, request, *args, **kwargs):
        if not _can_manage_payment_requests(request.user):
            messages.error(request, 'You do not have permission to view payment requests.')
            return redirect('finance_dashboard')
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        q      = self.request.GET.get('q', '').strip()
        status = self.request.GET.get('status', '').strip()
        rtype  = self.request.GET.get('rtype', '').strip()

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

        ctx.update({
            'payment_requests': qs[:100],
            'q': q,
            'status_filter': status,
            'rtype_filter': rtype,
            'status_choices': PaymentRequest.Status.choices,
            'rtype_choices': PaymentRequest.RecipientType.choices,
            'total': qs.aggregate(t=Sum('amount'))['t'] or Decimal('0'),
            'is_finance': _is_finance(self.request.user),
            'is_ceo': _is_ceo(self.request.user),
        })
        return ctx


class PaymentRequestCreateView(LoginRequiredMixin, View):
    template_name = 'finance/payment_request_form.html'

    def dispatch(self, request, *args, **kwargs):
        if not _can_manage_payment_requests(request.user):
            messages.error(request, 'You do not have permission to create payment requests.')
            return redirect('finance_dashboard')
        return super().dispatch(request, *args, **kwargs)

    def _ctx(self, request):
        from apps.core.models import User as CoreUser
        from apps.projects.models import Project
        return {
            'mode': 'create',
            'staff_list': CoreUser.objects.filter(is_active=True, status='active').order_by('first_name'),
            'budgets': Budget.objects.filter(status__in=['approved', 'active']).order_by('name'),
            'projects': Project.objects.filter(status='active').order_by('name'),
            'purchase_requests': PurchaseRequest.objects.exclude(
                status__in=['closed', 'rejected']
            ).order_by('-created_at')[:30],
            'payment_types': Payment.PaymentType.choices,
            'method_choices': Payment.Method.choices,
            'rtype_choices': PaymentRequest.RecipientType.choices,
            'doc_type_choices': PaymentRequestDocument.DocType.choices,
        }

    def get(self, request):
        return render(request, self.template_name, self._ctx(request))

    def post(self, request):
        try:
            amount = _validate_decimal(request.POST.get('amount'), 'amount')
        except ValueError as e:
            messages.error(request, str(e))
            return render(request, self.template_name, self._ctx(request))

        rtype = request.POST.get('recipient_type', PaymentRequest.RecipientType.EXTERNAL)
        recipient_user = None
        recipient_name = request.POST.get('recipient_name', '').strip()
        recipient_email = request.POST.get('recipient_email', '').strip()

        if rtype == PaymentRequest.RecipientType.STAFF:
            uid = request.POST.get('recipient_user')
            if uid:
                from apps.core.models import User as CoreUser
                recipient_user = CoreUser.objects.filter(pk=uid).first()
                if recipient_user:
                    recipient_name = getattr(recipient_user, 'full_name', str(recipient_user))
                    recipient_email = recipient_email or recipient_user.email

        pr = PaymentRequest.objects.create(
            title=request.POST.get('title', '').strip(),
            description=request.POST.get('description', '').strip(),
            payment_type=request.POST.get('payment_type', Payment.PaymentType.OTHER),
            recipient_type=rtype,
            recipient_user=recipient_user,
            recipient_name=recipient_name,
            recipient_email=recipient_email,
            recipient_notes=request.POST.get('recipient_notes', '').strip(),
            amount=amount,
            currency=request.POST.get('currency', 'GMD').strip().upper() or 'GMD',
            method=request.POST.get('method', Payment.Method.BANK_TRANSFER),
            due_date=request.POST.get('due_date') or None,
            budget_id=request.POST.get('budget') or None,
            project_id=request.POST.get('project') or None,
            purchase_request_id=request.POST.get('purchase_request') or None,
            status=request.POST.get('status', PaymentRequest.Status.DRAFT),
            requested_by=request.user,
            notes=request.POST.get('notes', '').strip(),
        )

        for key in request.FILES:
            if key.startswith('doc_file_'):
                idx = key[len('doc_file_'):]
                f = request.FILES[key]
                dtype = request.POST.get(f'doc_type_{idx}', PaymentRequestDocument.DocType.OTHER)
                dname = request.POST.get(f'doc_name_{idx}', f.name).strip() or f.name
                doc = PaymentRequestDocument(
                    payment_request=pr,
                    doc_type=dtype,
                    name=dname,
                    uploaded_by=request.user,
                )
                doc.file.save(f.name, f, save=True)

        if pr.status == PaymentRequest.Status.SENT:
            _send_payment_request_email(pr, request)

        messages.success(request, f'Payment request "{pr.title}" created.')
        return redirect('payment_request_detail', pk=pr.pk)


class PaymentRequestDetailView(LoginRequiredMixin, View):
    template_name = 'finance/payment_request_detail.html'

    def dispatch(self, request, *args, **kwargs):
        if not _can_manage_payment_requests(request.user):
            messages.error(request, 'You do not have permission to view this.')
            return redirect('finance_dashboard')
        return super().dispatch(request, *args, **kwargs)

    def get(self, request, pk):
        pr = get_object_or_404(PaymentRequest, pk=pk)
        return render(request, self.template_name, {
            'pr': pr,
            'documents': pr.documents.all(),
            'method_choices': Payment.Method.choices,
            'is_finance': _is_finance(request.user),
            'is_ceo': _is_ceo(request.user),
        })


class PaymentRequestMarkPaidView(LoginRequiredMixin, View):
    """Mark a payment request as paid, create Payment record, send confirmation."""

    def dispatch(self, request, *args, **kwargs):
        if not _is_finance(request.user):
            messages.error(request, 'Only Finance staff can mark payments as paid.')
            return redirect('finance_dashboard')
        return super().dispatch(request, *args, **kwargs)

    def post(self, request, pk):
        pr = get_object_or_404(PaymentRequest, pk=pk)

        if pr.status == PaymentRequest.Status.PAID:
            messages.warning(request, 'This payment request is already marked as paid.')
            return redirect('payment_request_detail', pk=pk)

        try:
            amount = _validate_decimal(request.POST.get('amount') or pr.amount, 'amount')
        except ValueError as e:
            messages.error(request, str(e))
            return redirect('payment_request_detail', pk=pk)

        payment_date = request.POST.get('payment_date') or timezone.now().date()
        method = request.POST.get('method') or pr.method
        notes = request.POST.get('notes', '').strip()
        currency = request.POST.get('currency') or pr.currency

        if pr.recipient_type == PaymentRequest.RecipientType.STAFF:
            direction = Payment.Direction.COMPANY_TO_STAFF
        else:
            direction = Payment.Direction.COMPANY_TO_VENDOR

        payment = Payment.objects.create(
            reference=_generate_reference('PREQ'),
            description=pr.title,
            purchase_request=pr.purchase_request,
            amount=amount,
            currency=currency,
            method=method,
            direction=direction,
            payment_type=pr.payment_type,
            paid_by=request.user,
            processed_by=request.user,
            approved_by=pr.approved_by or request.user,
            employee=pr.recipient_user,
            recipient=pr.effective_recipient_name,
            payment_date=payment_date,
            budget=pr.budget,
            project=pr.project,
            notes=notes,
        )

        if 'receipt' in request.FILES:
            payment.receipt = request.FILES['receipt']
            payment.save(update_fields=['receipt'])

        for key in request.FILES:
            if key.startswith('doc_file_'):
                idx = key[len('doc_file_'):]
                f = request.FILES[key]
                dtype = request.POST.get(f'doc_type_{idx}', PaymentRequestDocument.DocType.RECEIPT)
                dname = request.POST.get(f'doc_name_{idx}', f.name).strip() or f.name
                doc = PaymentRequestDocument(
                    payment_request=pr,
                    doc_type=dtype,
                    name=dname,
                    uploaded_by=request.user,
                )
                doc.file.save(f.name, f, save=True)

        _apply_payment_to_budget(payment)

        pr.status = PaymentRequest.Status.PAID
        pr.linked_payment = payment
        pr.paid_at = timezone.now()
        pr.approved_by = pr.approved_by or request.user
        pr.save(update_fields=['status', 'linked_payment', 'paid_at', 'approved_by', 'updated_at'])

        _send_payment_confirmation_email(pr)

        messages.success(
            request,
            f'"{pr.title}" marked as paid. Confirmation email sent to '
            f'{pr.effective_recipient_email or "recipient"}.',
        )
        return redirect('payment_request_detail', pk=pk)


class PaymentRequestSendView(LoginRequiredMixin, View):
    """Change status from Draft → Sent and email the recipient."""

    def post(self, request, pk):
        if not _can_manage_payment_requests(request.user):
            messages.error(request, 'Permission denied.')
            return redirect('finance_dashboard')

        pr = get_object_or_404(PaymentRequest, pk=pk)
        if pr.status != PaymentRequest.Status.DRAFT:
            messages.warning(request, 'Only draft requests can be sent.')
            return redirect('payment_request_detail', pk=pk)

        pr.status = PaymentRequest.Status.SENT
        pr.save(update_fields=['status', 'updated_at'])
        _send_payment_request_email(pr, request)

        messages.success(
            request,
            f'Payment request sent and email dispatched to '
            f'{pr.effective_recipient_email or pr.effective_recipient_name}.',
        )
        return redirect('payment_request_detail', pk=pk)


class PaymentRequestCancelView(LoginRequiredMixin, View):
    def post(self, request, pk):
        if not _can_manage_payment_requests(request.user):
            messages.error(request, 'Permission denied.')
            return redirect('finance_dashboard')
        pr = get_object_or_404(PaymentRequest, pk=pk)
        if pr.status == PaymentRequest.Status.PAID:
            messages.error(request, 'Paid requests cannot be cancelled.')
            return redirect('payment_request_detail', pk=pk)
        pr.status = PaymentRequest.Status.CANCELLED
        pr.save(update_fields=['status', 'updated_at'])
        messages.warning(request, f'Payment request "{pr.title}" cancelled.')
        return redirect('payment_request_list')


# ── Incoming Payment Request (outgoing invoices to customers) ────────────────

def _send_invoice_email(ipr, request=None):
    """Send a professional invoice email to the customer with all attachments."""
    from django.core.mail import EmailMessage

    if not ipr.customer_email:
        return

    office_name = getattr(settings, 'OFFICE_NAME', 'EasyOffice')
    org_name    = getattr(settings, 'ORGANISATION_NAME', office_name)
    org_email   = getattr(settings, 'DEFAULT_FROM_EMAIL',
                          f'finance@{org_name.lower().replace(" ", "")}.org')
    org_address = getattr(settings, 'ORGANISATION_ADDRESS', '')
    org_phone   = getattr(settings, 'ORGANISATION_PHONE', '')

    currency   = ipr.currency
    subtotal   = f'{ipr.amount:,.2f}'
    tax        = f'{ipr.tax_amount:,.2f}'
    discount   = f'{ipr.discount_amount:,.2f}'
    total      = f'{ipr.total_amount:,.2f}'
    due_date   = ipr.due_date.strftime('%d %B %Y') if ipr.due_date else 'Upon receipt'
    issue_date = ipr.issue_date.strftime('%d %B %Y')

    tax_row = (
        f'<tr><td style="padding:8px 0;color:#64748b">Tax / VAT</td>'
        f'<td style="text-align:right;padding:8px 0;color:#64748b">{currency} {tax}</td></tr>'
        if ipr.tax_amount else ''
    )
    disc_row = (
        f'<tr><td style="padding:8px 0;color:#64748b">Discount</td>'
        f'<td style="text-align:right;padding:8px 0;color:#ef4444">- {currency} {discount}</td></tr>'
        if ipr.discount_amount else ''
    )

    customer_block = ipr.customer_name
    if ipr.customer_company and ipr.customer_company != ipr.customer_name:
        customer_block = f'{ipr.customer_name}<br>{ipr.customer_company}'
    if ipr.customer_address:
        customer_block += (
            f'<br><span style="color:#94a3b8;font-size:13px">'
            f'{ipr.customer_address.replace(chr(10), "<br>")}</span>'
        )

    org_footer_block = org_name
    if org_address:
        org_footer_block += f'<br>{org_address}'
    if org_phone:
        org_footer_block += f'<br>{org_phone}'

    pay_inst_block = ''
    if ipr.payment_instructions:
        pay_inst_block = (
            f'<div style="background:#f0fdf4;border:1px solid #86efac;border-radius:12px;'
            f'padding:20px 24px;margin:24px 0">'
            f'<div style="font-size:13px;font-weight:700;text-transform:uppercase;'
            f'letter-spacing:.06em;color:#065f46;margin-bottom:10px">Payment Instructions</div>'
            f'<div style="font-size:14px;color:#1e293b;line-height:1.8;white-space:pre-line">'
            f'{ipr.payment_instructions}</div></div>'
        )

    desc_extra = (
        f'<br><br>{ipr.description.replace(chr(10), "<br>")}'
        if ipr.description else ''
    )
    org_addr_block = (
        org_address.replace(chr(10), '<br>')
        if org_address else ''
    )
    attach_note = (
        '<div style="font-size:13px;color:#64748b;margin-top:8px;font-style:italic">'
        '📎 Supporting documents are attached to this email.</div>'
        if ipr.documents.exists() else ''
    )

    html = f"""<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"/>
<style>
  body{{margin:0;padding:0;background:#f1f5f9;font-family:'Segoe UI',Arial,sans-serif;}}
  .wrapper{{max-width:660px;margin:32px auto;background:#fff;border-radius:16px;overflow:hidden;box-shadow:0 4px 24px rgba(0,0,0,.08);}}
  .header-inner{{background:linear-gradient(135deg,#1e3a5f 0%,#1d4ed8 100%);padding:36px 44px 28px;}}
  .header-inner h1{{margin:0 0 4px;font-size:28px;color:#fff;font-weight:900;letter-spacing:-.5px;}}
  .header-sub{{font-size:13px;color:rgba(255,255,255,.7);}}
  .inv-bar{{background:rgba(15,23,42,.5);padding:14px 44px;display:flex;justify-content:space-between;font-size:13px;}}
  .inv-bar .lbl{{color:rgba(255,255,255,.65);font-weight:600;text-transform:uppercase;letter-spacing:.05em;font-size:11px;}}
  .inv-bar .val{{color:#fff;font-weight:700;margin-top:3px;}}
  .body{{padding:36px 44px;}}
  .bill-grid{{display:grid;grid-template-columns:1fr 1fr;gap:24px;margin-bottom:32px;}}
  .bill-box .lbl{{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.06em;color:#94a3b8;margin-bottom:8px;}}
  .bill-box .val{{font-size:14px;color:#1e293b;line-height:1.7;}}
  .desc-box{{background:#f8fafc;border-radius:10px;padding:18px 20px;margin-bottom:28px;font-size:14px;color:#475569;line-height:1.7;}}
  .totals table{{width:100%;border-collapse:collapse;border-top:2px solid #e2e8f0;padding-top:16px;}}
  .total-row td{{font-size:16px;font-weight:800;color:#1e3a5f;padding:12px 0 0;border-top:2px solid #e2e8f0;}}
  .due-banner{{background:linear-gradient(135deg,#fffbeb,#fef3c7);border:1px solid #fde68a;border-radius:12px;padding:16px 20px;margin:24px 0;display:flex;align-items:center;gap:14px;}}
  .footer{{background:#f8fafc;padding:24px 44px;border-top:1px solid #e2e8f0;text-align:center;}}
  .footer p{{margin:0;font-size:12px;color:#94a3b8;line-height:1.8;}}
</style>
</head>
<body>
<div class="wrapper">
  <div class="header-inner">
    <h1>INVOICE</h1>
    <div class="header-sub">{org_name}</div>
  </div>
  <div class="inv-bar">
    <div><div class="lbl">Invoice No.</div><div class="val">{ipr.invoice_number}</div></div>
    <div><div class="lbl">Issue Date</div><div class="val">{issue_date}</div></div>
    <div><div class="lbl">Due Date</div><div class="val">{due_date}</div></div>
    <div><div class="lbl">Status</div><div class="val">AWAITING PAYMENT</div></div>
  </div>
  <div class="body">
    <div class="bill-grid">
      <div class="bill-box">
        <div class="lbl">From</div>
        <div class="val"><strong style="font-size:16px;color:#1e3a5f">{org_name}</strong><br>
          <span style="color:#64748b;font-size:13px">{org_addr_block}</span>
        </div>
      </div>
      <div class="bill-box">
        <div class="lbl">Bill To</div>
        <div class="val"><strong style="font-size:16px;color:#1e3a5f">{customer_block}</strong></div>
      </div>
    </div>
    <div style="font-size:13px;font-weight:700;text-transform:uppercase;letter-spacing:.06em;color:#94a3b8;margin-bottom:8px">Description</div>
    <div class="desc-box">
      <strong style="color:#1e293b">{ipr.title}</strong>{desc_extra}
    </div>
    <div class="totals">
      <table>
        <tr><td style="padding:8px 0;color:#475569">Subtotal</td><td style="text-align:right;padding:8px 0;color:#475569">{currency} {subtotal}</td></tr>
        {tax_row}
        {disc_row}
        <tr class="total-row"><td>Total Due</td><td style="text-align:right">{currency} {total}</td></tr>
      </table>
    </div>
    <div class="due-banner">
      <div style="font-size:24px">💳</div>
      <div>
        <div style="font-size:14px;color:#92400e">Amount due by <strong>{due_date}</strong></div>
        <div style="font-size:22px;font-weight:900;color:#78350f">{currency} {total}</div>
      </div>
    </div>
    {pay_inst_block}
    {attach_note}
  </div>
  <div class="footer">
    <p><strong>{org_name}</strong><br>
    {org_footer_block}<br>
    Please quote <strong>{ipr.invoice_number}</strong> in all correspondence.</p>
  </div>
</div>
</body>
</html>"""

    msg = EmailMessage(
        subject=f'Invoice {ipr.invoice_number} — {ipr.title} | {org_name}',
        body=html,
        from_email=org_email,
        to=[ipr.customer_email],
    )
    msg.content_subtype = 'html'

    for doc in ipr.documents.all():
        try:
            doc.file.open('rb')
            msg.attach(doc.name, doc.file.read(), 'application/octet-stream')
            doc.file.close()
        except Exception:
            pass

    try:
        msg.send()
    except Exception:
        pass


def _send_invoice_reminder_email(ipr):
    """Send an escalating payment reminder email to the customer."""
    from django.core.mail import EmailMessage

    if not ipr.customer_email:
        return

    office_name = getattr(settings, 'OFFICE_NAME', 'EasyOffice')
    org_name    = getattr(settings, 'ORGANISATION_NAME', office_name)
    org_email   = getattr(settings, 'DEFAULT_FROM_EMAIL',
                          f'finance@{org_name.lower().replace(" ", "")}.org')

    currency  = ipr.currency
    total     = f'{ipr.total_amount:,.2f}'
    due_date  = ipr.due_date.strftime('%d %B %Y') if ipr.due_date else 'as soon as possible'
    days_late = ipr.days_overdue
    count     = ipr.reminder_count + 1

    if count == 1:
        tone_subject = f'Friendly Reminder — Invoice {ipr.invoice_number} Due'
        tone_heading = 'Friendly Payment Reminder'
        tone_color   = '#3b82f6'
        tone_bg      = 'linear-gradient(135deg,#eff6ff,#dbeafe)'
        tone_border  = '#bfdbfe'
        tone_icon    = '📋'
        tone_body = (
            f'We hope this message finds you well. This is a friendly reminder that invoice '
            f'<strong>{ipr.invoice_number}</strong> for <strong>{currency} {total}</strong> '
            f'was due on <strong>{due_date}</strong> and has not yet been received.'
        )
        tone_closing = 'If you have already made this payment, please disregard this message and accept our thanks.'
    elif count == 2:
        tone_subject = f'Second Notice — Payment Overdue: Invoice {ipr.invoice_number}'
        tone_heading = 'Payment Overdue — Second Notice'
        tone_color   = '#f59e0b'
        tone_bg      = 'linear-gradient(135deg,#fffbeb,#fef3c7)'
        tone_border  = '#fde68a'
        tone_icon    = '⚠️'
        tone_body = (
            f'Our records show that invoice <strong>{ipr.invoice_number}</strong> for '
            f'<strong>{currency} {total}</strong>, due on <strong>{due_date}</strong>, '
            f'remains unpaid ({days_late} days overdue). We kindly request payment at your earliest convenience.'
        )
        tone_closing = 'Please contact us immediately if there is a dispute or if you require assistance.'
    else:
        tone_subject = f'URGENT — Final Notice: Invoice {ipr.invoice_number} Seriously Overdue'
        tone_heading = 'Final Payment Notice'
        tone_color   = '#ef4444'
        tone_bg      = 'linear-gradient(135deg,#fef2f2,#fee2e2)'
        tone_border  = '#fca5a5'
        tone_icon    = '🔴'
        tone_body = (
            f'Despite previous reminders, invoice <strong>{ipr.invoice_number}</strong> for '
            f'<strong>{currency} {total}</strong> remains unpaid. This account is now '
            f'<strong>{days_late} days overdue</strong>. Immediate payment is required. '
            f'Failure to settle may result in further action.'
        )
        tone_closing = 'Please contact us urgently to resolve this matter.'

    pay_inst_block = ''
    if ipr.payment_instructions:
        pay_inst_block = (
            f'<div style="background:#f0fdf4;border:1px solid #86efac;border-radius:12px;'
            f'padding:18px 22px;margin:24px 0">'
            f'<div style="font-size:12px;font-weight:700;text-transform:uppercase;'
            f'letter-spacing:.06em;color:#065f46;margin-bottom:8px">Payment Instructions</div>'
            f'<div style="font-size:14px;color:#1e293b;line-height:1.8;white-space:pre-line">'
            f'{ipr.payment_instructions}</div></div>'
        )

    overdue_row = (
        f'<tr><td>Days Overdue</td>'
        f'<td style="color:#ef4444;font-weight:700">{days_late} days</td></tr>'
        if days_late else ''
    )
    overdue_badge = (
        f'⚠ {days_late} days overdue' if days_late else f'Due: {due_date}'
    )

    html = f"""<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"/>
<style>
  body{{margin:0;padding:0;background:#f1f5f9;font-family:'Segoe UI',Arial,sans-serif;}}
  .wrapper{{max-width:620px;margin:32px auto;background:#fff;border-radius:16px;overflow:hidden;box-shadow:0 4px 24px rgba(0,0,0,.08);}}
  .header{{background:{tone_color};padding:32px 40px;text-align:center;}}
  .header .icon{{font-size:40px;margin-bottom:10px;display:block;}}
  .header h1{{margin:0;font-size:22px;color:#fff;font-weight:800;}}
  .header p{{margin:6px 0 0;font-size:13px;color:rgba(255,255,255,.8);}}
  .body{{padding:36px 40px;}}
  .summary-box{{background:{tone_bg};border:1px solid {tone_border};border-radius:12px;padding:20px 24px;margin:20px 0;text-align:center;}}
  .summary-box .inv{{font-size:13px;color:#64748b;margin-bottom:6px;}}
  .summary-box .amount{{font-size:32px;font-weight:900;color:#1e293b;}}
  .summary-box .due{{font-size:13px;color:{tone_color};font-weight:700;margin-top:4px;}}
  .detail-table{{width:100%;border-collapse:collapse;margin:20px 0;}}
  .detail-table td{{padding:10px 14px;font-size:14px;}}
  .detail-table tr:nth-child(odd) td{{background:#f8fafc;}}
  .detail-table td:first-child{{font-weight:700;color:#475569;width:40%;}}
  .footer{{background:#f8fafc;padding:22px 40px;border-top:1px solid #e2e8f0;text-align:center;}}
  .footer p{{margin:0;font-size:12px;color:#94a3b8;line-height:1.7;}}
</style>
</head>
<body>
<div class="wrapper">
  <div class="header">
    <span class="icon">{tone_icon}</span>
    <h1>{tone_heading}</h1>
    <p>{org_name} — Finance Department</p>
  </div>
  <div class="body">
    <p style="font-size:15px;color:#1e293b;line-height:1.7">Dear <strong>{ipr.customer_name}</strong>,</p>
    <p style="font-size:14px;color:#475569;line-height:1.8">{tone_body}</p>
    <div class="summary-box">
      <div class="inv">Invoice {ipr.invoice_number} · {ipr.title}</div>
      <div class="amount">{currency} {total}</div>
      <div class="due">{overdue_badge}</div>
    </div>
    <table class="detail-table">
      <tr><td>Invoice Number</td><td><strong>{ipr.invoice_number}</strong></td></tr>
      <tr><td>Amount Due</td><td><strong>{currency} {total}</strong></td></tr>
      <tr><td>Original Due Date</td><td>{due_date}</td></tr>
      {overdue_row}
      <tr><td>Reminder #</td><td>{count}</td></tr>
    </table>
    {pay_inst_block}
    <p style="font-size:14px;color:#475569;line-height:1.8">{tone_closing}</p>
    <p style="font-size:13px;color:#64748b;margin-top:16px">
      Please do not reply to this automated message — contact us directly at
      <a href="mailto:{org_email}" style="color:{tone_color}">{org_email}</a>.
    </p>
  </div>
  <div class="footer">
    <p><strong>{org_name}</strong> · Finance &amp; Administration<br>
    Invoice reference: {ipr.invoice_number}</p>
  </div>
</div>
</body>
</html>"""

    msg = EmailMessage(
        subject=tone_subject,
        body=html,
        from_email=org_email,
        to=[ipr.customer_email],
    )
    msg.content_subtype = 'html'

    for doc in ipr.documents.all():
        try:
            doc.file.open('rb')
            msg.attach(doc.name, doc.file.read(), 'application/octet-stream')
            doc.file.close()
        except Exception:
            pass

    try:
        msg.send()
    except Exception:
        pass


class IncomingPaymentRequestListView(LoginRequiredMixin, TemplateView):
    template_name = 'finance/incoming_payment_request_list.html'

    def dispatch(self, request, *args, **kwargs):
        if not _can_manage_payment_requests(request.user):
            messages.error(request, 'You do not have permission to view invoices.')
            return redirect('finance_dashboard')
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        q      = self.request.GET.get('q', '').strip()
        status = self.request.GET.get('status', '').strip()

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

        # Auto-mark overdue
        for ipr in qs:
            if ipr.is_overdue and ipr.status == IncomingPaymentRequest.Status.SENT:
                ipr.status = IncomingPaymentRequest.Status.OVERDUE
                ipr.save(update_fields=['status', 'updated_at'])

        total_outstanding = (
            qs.exclude(status__in=['paid', 'cancelled']).aggregate(t=Sum('amount'))['t']
            or Decimal('0')
        )

        ctx.update({
            'invoices':          qs[:100],
            'q':                 q,
            'status_filter':     status,
            'status_choices':    IncomingPaymentRequest.Status.choices,
            'total_outstanding': total_outstanding,
            'overdue_count':     qs.filter(status='overdue').count(),
            'paid_count':        qs.filter(status='paid').count(),
            'sent_count':        qs.filter(status='sent').count(),
            'is_finance':        _is_finance(self.request.user),
            'is_ceo':            _is_ceo(self.request.user),
        })
        return ctx


class IncomingPaymentRequestCreateView(LoginRequiredMixin, View):
    template_name = 'finance/incoming_payment_request_form.html'

    def dispatch(self, request, *args, **kwargs):
        if not _can_manage_payment_requests(request.user):
            messages.error(request, 'You do not have permission to create invoices.')
            return redirect('finance_dashboard')
        return super().dispatch(request, *args, **kwargs)

    def _ctx(self, request):
        from apps.projects.models import Project
        return {
            'mode':    'create',
            'budgets': Budget.objects.filter(status__in=['approved', 'active']).order_by('name'),
            'projects': Project.objects.filter(status='active').order_by('name'),
            'doc_type_choices': IncomingPaymentDocument.DocType.choices,
        }

    def get(self, request):
        return render(request, self.template_name, self._ctx(request))

    def post(self, request):
        try:
            amount = _validate_decimal(request.POST.get('amount'), 'amount')
            tax    = _validate_decimal(request.POST.get('tax_amount') or '0', 'tax')
            disc   = _validate_decimal(request.POST.get('discount_amount') or '0', 'discount')
        except ValueError as e:
            messages.error(request, str(e))
            return render(request, self.template_name, self._ctx(request))

        ipr = IncomingPaymentRequest.objects.create(
            title                = request.POST.get('title', '').strip(),
            description          = request.POST.get('description', '').strip(),
            customer_name        = request.POST.get('customer_name', '').strip(),
            customer_email       = request.POST.get('customer_email', '').strip(),
            customer_phone       = request.POST.get('customer_phone', '').strip(),
            customer_company     = request.POST.get('customer_company', '').strip(),
            customer_address     = request.POST.get('customer_address', '').strip(),
            amount               = amount,
            tax_amount           = tax,
            discount_amount      = disc,
            currency             = request.POST.get('currency', 'GMD').strip().upper() or 'GMD',
            issue_date           = request.POST.get('issue_date') or timezone.now().date(),
            due_date             = request.POST.get('due_date') or None,
            payment_instructions = request.POST.get('payment_instructions', '').strip(),
            notes                = request.POST.get('notes', '').strip(),
            project_id           = request.POST.get('project') or None,
            budget_id            = request.POST.get('budget') or None,
            status               = request.POST.get('status', IncomingPaymentRequest.Status.DRAFT),
            created_by           = request.user,
        )

        for key in request.FILES:
            if key.startswith('doc_file_'):
                idx   = key[len('doc_file_'):]
                f     = request.FILES[key]
                dtype = request.POST.get(f'doc_type_{idx}', IncomingPaymentDocument.DocType.INVOICE)
                dname = request.POST.get(f'doc_name_{idx}', f.name).strip() or f.name
                doc   = IncomingPaymentDocument(
                    payment_request=ipr,
                    doc_type=dtype,
                    name=dname,
                    uploaded_by=request.user,
                )
                doc.file.save(f.name, f, save=True)

        if ipr.status == IncomingPaymentRequest.Status.SENT:
            _send_invoice_email(ipr, request)
            messages.success(
                request,
                f'Invoice {ipr.invoice_number} created and emailed to {ipr.customer_email}.',
            )
        else:
            messages.success(request, f'Invoice {ipr.invoice_number} saved as draft.')

        return redirect('incoming_payment_request_detail', pk=ipr.pk)


def _linked_invoice_documents(ipr):
    """
    Every document generated in the Invoice Builder app (apps.invoices)
    that belongs to this incoming payment request:

      1. InvoiceDocuments linked directly via `linked_receivable` — the
         finalized invoice(s) that created this receivable.
      2. The whole conversion family of those documents — delivery notes,
         proformas, quotations, receipts converted from or into them.
         Self-referential links on InvoiceDocument are discovered through
         model introspection, so this keeps working whatever the FK is
         called (converted_from, source_document, …).

    Returns a list sorted newest-first. Never raises — an empty list is
    returned if the invoices app is unavailable.
    """
    try:
        from apps.invoices.models import InvoiceDocument
    except Exception:
        return []

    try:
        docs = list(
            InvoiceDocument.objects
            .filter(linked_receivable=ipr)
            .order_by('-created_at')
        )
    except Exception:
        return []

    seen = {d.pk for d in docs}

    # Self-referential relations (conversion chains) on InvoiceDocument.
    forward_fks, reverse_accessors = [], []
    try:
        for f in InvoiceDocument._meta.get_fields():
            if not getattr(f, 'is_relation', False):
                continue
            if f.related_model is not InvoiceDocument:
                continue
            if getattr(f, 'many_to_one', False):        # e.g. converted_from
                forward_fks.append(f.name)
            elif getattr(f, 'one_to_many', False):      # reverse of the above
                reverse_accessors.append(f.get_accessor_name())
    except Exception:
        pass

    frontier = list(docs)
    while frontier:
        d = frontier.pop()
        related = []
        for name in forward_fks:
            try:
                obj = getattr(d, name, None)
                if obj is not None:
                    related.append(obj)
            except Exception:
                pass
        for accessor in reverse_accessors:
            try:
                mgr = getattr(d, accessor, None)
                if mgr is not None and hasattr(mgr, 'all'):
                    related.extend(mgr.all())
            except Exception:
                pass
        for r in related:
            if r.pk not in seen:
                seen.add(r.pk)
                docs.append(r)
                frontier.append(r)

    docs.sort(key=lambda d: d.created_at or timezone.now(), reverse=True)
    return docs


class IncomingPaymentRequestDetailView(LoginRequiredMixin, View):
    template_name = 'finance/incoming_payment_request_detail.html'

    def dispatch(self, request, *args, **kwargs):
        if not _can_manage_payment_requests(request.user):
            messages.error(request, 'Permission denied.')
            return redirect('finance_dashboard')
        return super().dispatch(request, *args, **kwargs)

    def get(self, request, pk):
        ipr = get_object_or_404(IncomingPaymentRequest, pk=pk)

        # ── Follow-up tasks linked to this invoice ──────────────────────
        # Open tasks first (todo, in_progress, on_hold, review), with
        # done/cancelled at the bottom.
        followup_tasks = list(
            ipr.followup_tasks
            .select_related('assigned_to')
            .order_by('status', 'due_date', '-created_at')
        )

        # ── Assignee dropdown ───────────────────────────────────────────
        # Same population strategy as TaskCreateView.
        assignable_users = (
            User.objects
            .filter(is_active=True, status='active')
            .order_by('first_name', 'last_name')
        )

        return render(request, self.template_name, {
            'ipr': ipr,
            'documents': ipr.documents.all(),
            'invoice_documents': _linked_invoice_documents(ipr),
            'is_finance': _is_finance(request.user),
            'is_ceo': _is_ceo(request.user),
            'doc_type_choices': IncomingPaymentDocument.DocType.choices,

            # ── New keys for Payment Follow-up card ─────────────────────
            'followup_tasks': followup_tasks,
            'assignable_users': assignable_users,
            'can_assign_followup': _can_assign_followup(request.user, ipr),
        })


class IncomingPaymentRequestSendView(LoginRequiredMixin, View):
    """Send / resend the invoice email to the customer."""

    def post(self, request, pk):
        if not _can_manage_payment_requests(request.user):
            messages.error(request, 'Permission denied.')
            return redirect('finance_dashboard')

        ipr = get_object_or_404(IncomingPaymentRequest, pk=pk)
        if ipr.status == IncomingPaymentRequest.Status.PAID:
            messages.warning(request, 'Invoice is already paid.')
            return redirect('incoming_payment_request_detail', pk=pk)

        ipr.status = IncomingPaymentRequest.Status.SENT
        ipr.save(update_fields=['status', 'updated_at'])
        _send_invoice_email(ipr, request)
        messages.success(
            request,
            f'Invoice {ipr.invoice_number} emailed to {ipr.customer_email or ipr.customer_name}.',
        )
        return redirect('incoming_payment_request_detail', pk=pk)


class IncomingPaymentRequestReminderView(LoginRequiredMixin, View):
    """Send a payment reminder — escalates in tone on each call."""

    def post(self, request, pk):
        if not _can_manage_payment_requests(request.user):
            messages.error(request, 'Permission denied.')
            return redirect('finance_dashboard')

        ipr = get_object_or_404(IncomingPaymentRequest, pk=pk)
        if ipr.status in (
            IncomingPaymentRequest.Status.PAID,
            IncomingPaymentRequest.Status.CANCELLED,
        ):
            messages.warning(request, 'Cannot send reminder for a paid or cancelled invoice.')
            return redirect('incoming_payment_request_detail', pk=pk)

        _send_invoice_reminder_email(ipr)
        ipr.reminder_count    += 1
        ipr.last_reminder_sent = timezone.now()
        if ipr.status == IncomingPaymentRequest.Status.DRAFT:
            ipr.status = IncomingPaymentRequest.Status.SENT
        ipr.save(update_fields=['reminder_count', 'last_reminder_sent', 'status', 'updated_at'])

        ordinals = {1: '1st', 2: '2nd', 3: '3rd'}
        label = ordinals.get(ipr.reminder_count, f'{ipr.reminder_count}th')
        messages.success(
            request,
            f'{label} reminder emailed to {ipr.customer_email or ipr.customer_name}.',
        )
        return redirect('incoming_payment_request_detail', pk=pk)


class IncomingPaymentRequestMarkPaidView(LoginRequiredMixin, View):
    """Record that the customer has paid."""

    def post(self, request, pk):
        if not _is_finance(request.user):
            messages.error(request, 'Only Finance staff can mark invoices as paid.')
            return redirect('finance_dashboard')

        ipr = get_object_or_404(IncomingPaymentRequest, pk=pk)
        if ipr.status == IncomingPaymentRequest.Status.PAID:
            messages.warning(request, 'Invoice already marked as paid.')
            return redirect('incoming_payment_request_detail', pk=pk)

        ipr.status  = IncomingPaymentRequest.Status.PAID
        ipr.paid_at = timezone.now()
        ipr.save(update_fields=['status', 'paid_at', 'updated_at'])

        messages.success(
            request,
            f'Invoice {ipr.invoice_number} marked as paid. '
            f'Amount: {ipr.currency} {ipr.total_amount:,.2f}.',
        )
        return redirect('incoming_payment_request_detail', pk=pk)


class IncomingPaymentRequestAddDocView(LoginRequiredMixin, View):
    """Upload additional documents to an existing invoice."""

    def post(self, request, pk):
        if not _can_manage_payment_requests(request.user):
            messages.error(request, 'Permission denied.')
            return redirect('finance_dashboard')

        ipr = get_object_or_404(IncomingPaymentRequest, pk=pk)
        for key in request.FILES:
            if key.startswith('doc_file_'):
                idx   = key[len('doc_file_'):]
                f     = request.FILES[key]
                dtype = request.POST.get(f'doc_type_{idx}', IncomingPaymentDocument.DocType.OTHER)
                dname = request.POST.get(f'doc_name_{idx}', f.name).strip() or f.name
                doc   = IncomingPaymentDocument(
                    payment_request=ipr,
                    doc_type=dtype,
                    name=dname,
                    uploaded_by=request.user,
                )
                doc.file.save(f.name, f, save=True)

        messages.success(request, 'Documents uploaded.')
        return redirect('incoming_payment_request_detail', pk=pk)


class IncomingPaymentRequestCancelView(LoginRequiredMixin, View):
    def post(self, request, pk):
        if not _can_manage_payment_requests(request.user):
            messages.error(request, 'Permission denied.')
            return redirect('finance_dashboard')
        ipr = get_object_or_404(IncomingPaymentRequest, pk=pk)
        if ipr.status == IncomingPaymentRequest.Status.PAID:
            messages.error(request, 'Paid invoices cannot be cancelled.')
            return redirect('incoming_payment_request_detail', pk=pk)
        ipr.status = IncomingPaymentRequest.Status.CANCELLED
        ipr.save(update_fields=['status', 'updated_at'])
        messages.warning(request, f'Invoice {ipr.invoice_number} cancelled.')
        return redirect('incoming_payment_request_list')


# ── Contract Views ───────────────────────────────────────────────────────────

class ContractDashboardView(LoginRequiredMixin, TemplateView):
    template_name = 'finance/contract_dashboard.html'

    def dispatch(self, request, *args, **kwargs):
        if not _can_view_contracts(request.user):
            return HttpResponseForbidden('You do not have permission to view contracts.')
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        today = timezone.now().date()

        contracts = Contract.objects.select_related(
            'staff', 'project', 'budget', 'created_by'
        ).order_by('end_date', '-created_at')

        expiring = [
            c for c in contracts
            if c.days_to_end >= 0 and c.days_to_end <= c.alert_days_before_end
        ]
        expired = [c for c in contracts if c.is_expired]
        due_for_invoice = [
            c for c in contracts
            if (
                c.auto_generate_invoice
                and c.next_invoice_date
                and c.next_invoice_date <= today
                and c.status not in [Contract.Status.TERMINATED, Contract.Status.EXPIRED]
            )
        ]

        ctx.update({
            'contracts':           contracts[:30],
            'expiring_contracts':  expiring[:20],
            'expired_contracts':   expired[:20],
            'due_invoice_contracts': due_for_invoice[:20],
            'total_contracts':     contracts.count(),
            'active_contracts':    contracts.filter(status=Contract.Status.ACTIVE).count(),
            'staff_contracts':     contracts.filter(contract_type=Contract.ContractType.STAFF).count(),
            'vendor_contracts':    contracts.filter(contract_type=Contract.ContractType.VENDOR).count(),
            'is_finance':          _is_finance(self.request.user),
            'is_hr':               _is_hr(self.request.user),
            'is_ceo':              _is_ceo(self.request.user),
        })
        return ctx


class ContractListView(LoginRequiredMixin, TemplateView):
    template_name = 'finance/contract_list.html'

    def dispatch(self, request, *args, **kwargs):
        if not _can_view_contracts(request.user):
            return HttpResponseForbidden('You do not have permission to view contracts.')
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        q      = self.request.GET.get('q', '').strip()
        ctype  = self.request.GET.get('contract_type', '').strip()
        status = self.request.GET.get('status', '').strip()

        qs = Contract.objects.select_related(
            'staff', 'project', 'budget', 'created_by'
        ).order_by('end_date', '-created_at')

        if q:
            qs = qs.filter(
                Q(title__icontains=q)
                | Q(reference__icontains=q)
                | Q(vendor_name__icontains=q)
                | Q(vendor_company__icontains=q)
            )
        if ctype:
            qs = qs.filter(contract_type=ctype)
        if status:
            qs = qs.filter(status=status)

        ctx.update({
            'contracts':             qs,
            'q':                     q,
            'type_filter':           ctype,
            'status_filter':         status,
            'contract_type_choices': Contract.ContractType.choices,
            'status_choices':        Contract.Status.choices,
            'is_finance':            _is_finance(self.request.user),
            'is_ceo':                _is_ceo(self.request.user),
        })
        return ctx


class ContractCreateView(LoginRequiredMixin, View):
    template_name = 'finance/contract_form.html'

    def dispatch(self, request, *args, **kwargs):
        if not _can_manage_contracts(request.user):
            return HttpResponseForbidden('You do not have permission to manage contracts.')
        return super().dispatch(request, *args, **kwargs)

    def _get_form_context(self, contract=None):
        from apps.projects.models import Project
        user = self.request.user
        return {
            'contract':              contract,
            'is_edit':               False,
            'contract_type_choices': Contract.ContractType.choices,
            'status_choices':        Contract.Status.choices,
            'billing_cycle_choices': Contract.BillingCycle.choices,
            'staff_users':           User.objects.filter(is_active=True).order_by('first_name', 'last_name'),
            'projects':              Project.objects.order_by('name'),
            'budgets':               Budget.objects.order_by('-fiscal_year', 'name'),
            # Templates for the default_invoice_template picker
            'invoice_templates':     _user_template_qs(user).order_by('name'),
            'is_finance':            _is_finance(user),
            'is_ceo':                _is_ceo(user),
            'is_hr':                 _is_hr(user),
        }

    def get(self, request):
        return render(request, self.template_name, self._get_form_context())

    def post(self, request):
        try:
            standard_cost = Decimal(str(request.POST.get('standard_cost') or '0'))
        except Exception:
            messages.error(request, 'Please enter a valid standard cost.')
            return render(request, self.template_name, self._get_form_context())

        title         = request.POST.get('title', '').strip()
        contract_type = request.POST.get('contract_type')

        if not title:
            messages.error(request, 'Title is required.')
            return render(request, self.template_name, self._get_form_context())

        if contract_type not in dict(Contract.ContractType.choices):
            messages.error(request, 'Please select a valid contract type.')
            return render(request, self.template_name, self._get_form_context())

        # Resolve default_invoice_template if provided
        tmpl_id  = request.POST.get('default_invoice_template') or None
        template = None
        if tmpl_id:
            template = _user_template_qs(request.user).filter(pk=tmpl_id).first()

        # Coerce date fields into real date objects so post_save signals
        # don't choke on strings.
        try:
            start_date        = _parse_date(request.POST.get('start_date'),        'start date')
            end_date          = _parse_date(request.POST.get('end_date'),          'end date')
            renewal_date      = _parse_date(request.POST.get('renewal_date'),      'renewal date')
            next_invoice_date = _parse_date(request.POST.get('next_invoice_date'), 'next invoice date')
        except ValueError as e:
            messages.error(request, str(e))
            return render(request, self.template_name, self._get_form_context())

        if not start_date or not end_date:
            messages.error(request, 'Both start date and end date are required.')
            return render(request, self.template_name, self._get_form_context())

        contract = Contract.objects.create(
            title                    = title,
            contract_type            = contract_type,
            staff_id                 = request.POST.get('staff') or None,
            vendor_name              = request.POST.get('vendor_name', '').strip(),
            vendor_email             = request.POST.get('vendor_email', '').strip(),
            vendor_phone             = request.POST.get('vendor_phone', '').strip(),
            vendor_company           = request.POST.get('vendor_company', '').strip(),
            vendor_address           = request.POST.get('vendor_address', '').strip(),
            reference                = request.POST.get('reference', '').strip() or _next_contract_reference(),
            description              = request.POST.get('description', '').strip(),
            start_date               = start_date,
            end_date                 = end_date,
            renewal_date             = renewal_date,
            status                   = request.POST.get('status') or Contract.Status.ACTIVE,
            project_id               = request.POST.get('project') or None,
            budget_id                = request.POST.get('budget') or None,
            standard_cost            = standard_cost,
            currency                 = request.POST.get('currency', 'GMD'),
            billing_cycle            = request.POST.get('billing_cycle', Contract.BillingCycle.ONE_OFF),
            auto_generate_invoice    = request.POST.get('auto_generate_invoice') == 'on',
            auto_send_invoice        = request.POST.get('auto_send_invoice') == 'on',
            next_invoice_date        = next_invoice_date,
            default_invoice_template = template,
            alert_days_before_end    = request.POST.get('alert_days_before_end') or 30,
            alert_days_before_renewal= request.POST.get('alert_days_before_renewal') or 14,
            send_expiry_alerts       = request.POST.get('send_expiry_alerts') == 'on',
            send_renewal_alerts      = request.POST.get('send_renewal_alerts') == 'on',
            created_by               = request.user,
            updated_by               = request.user,
        )

        if 'document' in request.FILES:
            contract.document = request.FILES['document']
            contract.save(update_fields=['document'])

        messages.success(request, f'Contract "{contract.title}" created.')
        return redirect('contract_detail', pk=contract.pk)


class ContractUpdateView(LoginRequiredMixin, View):
    template_name = 'finance/contract_form.html'

    def dispatch(self, request, *args, **kwargs):
        if not _can_manage_contracts(request.user):
            return HttpResponseForbidden('You do not have permission to manage contracts.')
        self.contract = get_object_or_404(Contract, pk=kwargs['pk'])
        return super().dispatch(request, *args, **kwargs)

    def _get_form_context(self, contract):
        from apps.projects.models import Project
        user = self.request.user
        return {
            'contract':              contract,
            'is_edit':               True,
            'contract_type_choices': Contract.ContractType.choices,
            'status_choices':        Contract.Status.choices,
            'billing_cycle_choices': Contract.BillingCycle.choices,
            'staff_users':           User.objects.filter(is_active=True).order_by('first_name', 'last_name'),
            'projects':              Project.objects.order_by('name'),
            'budgets':               Budget.objects.order_by('-fiscal_year', 'name'),
            'invoice_templates':     _user_template_qs(user).order_by('name'),
            'is_finance':            _is_finance(user),
            'is_ceo':                _is_ceo(user),
            'is_hr':                 _is_hr(user),
        }

    def get(self, request, pk):
        return render(request, self.template_name, self._get_form_context(self.contract))

    def post(self, request, pk):
        try:
            standard_cost = Decimal(str(request.POST.get('standard_cost') or '0'))
        except Exception:
            messages.error(request, 'Please enter a valid standard cost.')
            return render(request, self.template_name, self._get_form_context(self.contract))

        title         = request.POST.get('title', '').strip()
        contract_type = request.POST.get('contract_type')

        if not title:
            messages.error(request, 'Title is required.')
            return render(request, self.template_name, self._get_form_context(self.contract))

        if contract_type not in dict(Contract.ContractType.choices):
            messages.error(request, 'Please select a valid contract type.')
            return render(request, self.template_name, self._get_form_context(self.contract))

        # Resolve default_invoice_template
        tmpl_id  = request.POST.get('default_invoice_template') or None
        template = None
        if tmpl_id:
            template = _user_template_qs(request.user).filter(pk=tmpl_id).first()

        # Coerce date fields into real date objects so post_save signals
        # don't choke on raw POST strings.
        try:
            start_date        = _parse_date(request.POST.get('start_date'),        'start date')
            end_date          = _parse_date(request.POST.get('end_date'),          'end date')
            renewal_date      = _parse_date(request.POST.get('renewal_date'),      'renewal date')
            next_invoice_date = _parse_date(request.POST.get('next_invoice_date'), 'next invoice date')
        except ValueError as e:
            messages.error(request, str(e))
            return render(request, self.template_name, self._get_form_context(self.contract))

        if not start_date or not end_date:
            messages.error(request, 'Both start date and end date are required.')
            return render(request, self.template_name, self._get_form_context(self.contract))

        c = self.contract
        c.title                     = title
        c.contract_type             = contract_type
        c.staff_id                  = request.POST.get('staff') or None
        c.vendor_name               = request.POST.get('vendor_name', '').strip()
        c.vendor_email              = request.POST.get('vendor_email', '').strip()
        c.vendor_phone              = request.POST.get('vendor_phone', '').strip()
        c.vendor_company            = request.POST.get('vendor_company', '').strip()
        c.vendor_address            = request.POST.get('vendor_address', '').strip()
        c.reference                 = request.POST.get('reference', '').strip() or c.reference
        c.description               = request.POST.get('description', '').strip()
        c.start_date                = start_date
        c.end_date                  = end_date
        c.renewal_date              = renewal_date
        c.status                    = request.POST.get('status') or c.status
        c.project_id                = request.POST.get('project') or None
        c.budget_id                 = request.POST.get('budget') or None
        c.standard_cost             = standard_cost
        c.currency                  = request.POST.get('currency', 'GMD')
        c.billing_cycle             = request.POST.get('billing_cycle', Contract.BillingCycle.ONE_OFF)
        c.auto_generate_invoice     = request.POST.get('auto_generate_invoice') == 'on'
        c.auto_send_invoice         = request.POST.get('auto_send_invoice') == 'on'
        c.next_invoice_date         = next_invoice_date
        c.default_invoice_template  = template  # None clears it; a resolved obj sets it
        c.alert_days_before_end     = request.POST.get('alert_days_before_end') or 30
        c.alert_days_before_renewal = request.POST.get('alert_days_before_renewal') or 14
        c.send_expiry_alerts        = request.POST.get('send_expiry_alerts') == 'on'
        c.send_renewal_alerts       = request.POST.get('send_renewal_alerts') == 'on'
        c.updated_by                = request.user

        if 'document' in request.FILES:
            c.document = request.FILES['document']

        c.save()
        messages.success(request, f'Contract "{c.title}" updated.')
        return redirect('contract_detail', pk=c.pk)


class ContractDetailView(LoginRequiredMixin, DetailView):
    model = Contract
    template_name = 'finance/contract_detail.html'
    context_object_name = 'contract'

    def dispatch(self, request, *args, **kwargs):
        if not _can_view_contracts(request.user):
            return HttpResponseForbidden('You do not have permission to view contracts.')
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        user = self.request.user
        contract = self.object

        # Invoice history — newest first, with all related data pre-fetched
        invoice_links = (
            contract.invoice_links
            .select_related('invoice', 'invoice__generated_pdf', 'generated_by')
            .order_by('-created_at')
        )

        # Extension history — chronological (oldest first) so the user
        # reads -1, -2, -3 … top to bottom.
        extensions = (
            contract.extensions
            .select_related('created_by')
            .order_by('extension_number')
        )

        # Documents (generated + uploaded wrappers) — newest first.
        documents = (
            contract.documents
            .select_related('generated_by')
            .order_by('-created_at')
        )
        latest_generated = documents.filter(
            source=ContractDocument.Source.GENERATED
        ).first()

        # Signature requests — newest first.
        signature_requests = (
            contract.signature_requests
            .select_related('document', 'signature', 'created_by', 'voided_by')
            .order_by('-created_at')
        )
        # The "active" signing request, if any (non-final state).
        active_signature_request = signature_requests.filter(
            status__in=[
                ContractSignatureRequest.Status.SENT,
                ContractSignatureRequest.Status.VIEWED,
            ]
        ).first()
        latest_signed = signature_requests.filter(
            status=ContractSignatureRequest.Status.SIGNED
        ).first()

        ctx.update({
            'invoice_links':       invoice_links,
            'extensions':          extensions,
            'extension_count':     extensions.count(),
            'next_extension_no':   (extensions.count() + 1),
            'documents':                 documents,
            'latest_generated_document': latest_generated,
            'has_uploaded_document':     bool(contract.document),
            'signature_requests':        signature_requests,
            'active_signature_request':  active_signature_request,
            'latest_signed_request':     latest_signed,
            'can_extend_contract': (
                _can_manage_contracts(user)
                and contract.status not in [
                    Contract.Status.TERMINATED,
                    Contract.Status.COMPLETED,
                ]
            ),
            'can_manage_documents': _can_manage_contracts(user),
            'can_generate_invoice': (
                _user_can_manage_contract_invoices(user)
                and contract.status == Contract.Status.ACTIVE
            ),
            # Templates available in the "Generate Invoice" modal
            'available_templates': (
                _user_template_qs(user).order_by('-usage_count', 'name')
                if can_use_invoices(user) else []
            ),
            'default_template':    contract.default_invoice_template,
            'is_finance':          _is_finance(user),
            'is_ceo':              _is_ceo(user),
            'is_hr':               _is_hr(user),
        })
        return ctx


class ContractGenerateInvoiceView(LoginRequiredMixin, View):
    """
    POST /finance/contracts/<uuid:pk>/generate-invoice/

    Form fields:
        template_id  — UUID of an InvoiceTemplate (optional; falls back to
                        contract.default_invoice_template).
        finalize     — '1' to finalize immediately (default), '0' for draft.

    On success → redirects to the new invoice's detail page in the invoice app.
    On failure → redirects back to the contract detail with a flashed error.
    """

    def post(self, request, pk):
        if not _user_can_manage_contract_invoices(request.user):
            return HttpResponseForbidden(
                "You don't have permission to generate invoices for contracts."
            )

        contract = get_object_or_404(Contract, pk=pk)

        # Resolve template: explicit POST override first, then contract default
        template = None
        tmpl_id  = (request.POST.get('template_id') or '').strip()
        if tmpl_id:
            template = _user_template_qs(request.user).filter(pk=tmpl_id).first()
            if template is None:
                messages.error(request, 'Selected invoice template is not available to you.')
                return redirect('contract_detail', pk=contract.pk)

        finalize = request.POST.get('finalize', '1') != '0'

        try:
            result = generate_invoice_for_contract(
                contract,
                actor=request.user,
                template=template,   # None → service uses contract.default_invoice_template
                finalize=finalize,
                source='manual',
            )
        except ContractInvoiceError as e:
            messages.error(request, str(e))
            return redirect('contract_detail', pk=contract.pk)
        except Exception as e:
            messages.error(request, f'Could not generate invoice: {e}')
            return redirect('contract_detail', pk=contract.pk)

        if result.was_finalized:
            messages.success(
                request,
                f'Invoice {result.invoice.number} generated and finalized for {contract.title}.',
            )
        else:
            messages.success(
                request,
                f'Draft invoice created for {contract.title}. '
                f'Review and finalize it in the invoice module.',
            )

        return redirect(
            reverse('invoices:invoice_detail', kwargs={'pk': result.invoice.pk})
        )

# ════════════════════════════════════════════════════════════════════════════
# Contract Extensions
# ════════════════════════════════════════════════════════════════════════════

class ContractExtendView(LoginRequiredMixin, View):
    """
    POST /finance/contracts/<uuid:pk>/extend/

    Creates a ContractExtension row for the contract, advances the parent
    contract's `end_date` to the new end date, and (optionally) clears
    the EXPIRED status back to ACTIVE.

    Form fields:
        new_end_date       — required, ISO date (YYYY-MM-DD); must be > current end_date.
        effective_date     — optional, ISO date; defaults to today.
        reason             — optional, free text.
        notes              — optional, free text.
        additional_amount  — optional decimal; defaults to 0.
        currency           — optional; defaults to the contract's currency.
        document           — optional file upload (signed addendum).
    """

    def dispatch(self, request, *args, **kwargs):
        if not _can_manage_contracts(request.user):
            return HttpResponseForbidden(
                'You do not have permission to extend contracts.'
            )
        self.contract = get_object_or_404(Contract, pk=kwargs['pk'])
        return super().dispatch(request, *args, **kwargs)

    def post(self, request, pk):
        c = self.contract

        if c.status in [Contract.Status.TERMINATED, Contract.Status.COMPLETED]:
            messages.error(
                request,
                f'Cannot extend a {c.get_status_display().lower()} contract.',
            )
            return redirect('contract_detail', pk=c.pk)

        # ── Parse new_end_date ──────────────────────────────────────────────
        new_end_raw = (request.POST.get('new_end_date') or '').strip()
        if not new_end_raw:
            messages.error(request, 'A new end date is required to extend the contract.')
            return redirect('contract_detail', pk=c.pk)

        from datetime import datetime as _dt
        try:
            new_end_date = _dt.strptime(new_end_raw, '%Y-%m-%d').date()
        except ValueError:
            messages.error(request, 'Please enter the new end date in YYYY-MM-DD format.')
            return redirect('contract_detail', pk=c.pk)

        if new_end_date <= c.end_date:
            messages.error(
                request,
                f'New end date must be after the current end date '
                f'({c.end_date.strftime("%b %-d, %Y")}).',
            )
            return redirect('contract_detail', pk=c.pk)

        # ── Parse optional effective_date ───────────────────────────────────
        eff_raw = (request.POST.get('effective_date') or '').strip()
        effective_date = timezone.now().date()
        if eff_raw:
            try:
                effective_date = _dt.strptime(eff_raw, '%Y-%m-%d').date()
            except ValueError:
                messages.error(request, 'Please enter the effective date in YYYY-MM-DD format.')
                return redirect('contract_detail', pk=c.pk)

        # ── Parse optional money delta ──────────────────────────────────────
        try:
            additional_amount = Decimal(str(request.POST.get('additional_amount') or '0'))
        except (InvalidOperation, ValueError):
            messages.error(request, 'Please enter a valid additional amount.')
            return redirect('contract_detail', pk=c.pk)

        currency = (request.POST.get('currency') or '').strip() or c.currency

        previous_end_date = c.end_date

        # ── Create the extension row ────────────────────────────────────────
        ext = ContractExtension(
            contract          = c,
            previous_end_date = previous_end_date,
            new_end_date      = new_end_date,
            effective_date    = effective_date,
            additional_amount = additional_amount,
            currency          = currency,
            reason            = (request.POST.get('reason') or '').strip(),
            notes             = (request.POST.get('notes') or '').strip(),
            created_by        = request.user,
        )
        if 'document' in request.FILES:
            ext.document = request.FILES['document']
        ext.save()

        # ── Advance the parent contract ─────────────────────────────────────
        c.end_date = new_end_date
        # If it had already expired, bring it back to ACTIVE.
        if c.status in [Contract.Status.EXPIRED, Contract.Status.EXPIRING]:
            c.status = Contract.Status.ACTIVE
        c.updated_by = request.user
        c.save(update_fields=['end_date', 'status', 'updated_by', 'updated_at'])

        messages.success(
            request,
            f'Contract extended. New reference {ext.reference}, '
            f'new end date {new_end_date.strftime("%b %-d, %Y")}.',
        )
        return redirect('contract_detail', pk=c.pk)


class ContractExtensionDeleteView(LoginRequiredMixin, View):
    """
    POST /finance/contracts/<uuid:pk>/extensions/<uuid:ext_pk>/delete/

    Allows the most-recent extension to be removed (e.g. created in error).
    Rolls the parent contract's end_date back to that extension's
    `previous_end_date`.

    Only the latest extension is deletable, to keep the N-1, N-2, …
    numbering contiguous.
    """

    def dispatch(self, request, *args, **kwargs):
        if not _can_manage_contracts(request.user):
            return HttpResponseForbidden(
                'You do not have permission to manage contract extensions.'
            )
        self.contract = get_object_or_404(Contract, pk=kwargs['pk'])
        self.extension = get_object_or_404(
            ContractExtension,
            pk=kwargs['ext_pk'],
            contract=self.contract,
        )
        return super().dispatch(request, *args, **kwargs)

    def post(self, request, pk, ext_pk):
        c = self.contract
        ext = self.extension

        latest = (
            c.extensions.order_by('-extension_number').first()
        )
        if not latest or latest.pk != ext.pk:
            messages.error(
                request,
                'Only the most recent extension can be removed. '
                'Remove later extensions first.',
            )
            return redirect('contract_detail', pk=c.pk)

        # Roll the end date back.
        c.end_date = ext.previous_end_date
        c.updated_by = request.user
        c.save(update_fields=['end_date', 'updated_by', 'updated_at'])

        ref = ext.reference
        ext.delete()

        messages.success(
            request,
            f'Extension {ref} removed. Contract end date rolled back to '
            f'{c.end_date.strftime("%b %-d, %Y")}.',
        )
        return redirect('contract_detail', pk=c.pk)


# ════════════════════════════════════════════════════════════════════════════
# Contract Documents & E-Signature
# ════════════════════════════════════════════════════════════════════════════

from django.http import FileResponse, Http404, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
from django.utils.decorators import method_decorator
from django.core.files.base import ContentFile
import base64 as _b64
import re as _re

from .contract_document_service import (
    generate_contract_document,
    register_uploaded_document,
    build_signature_request,
    mark_request_sent,
    mark_request_viewed,
    stamp_signature_onto_pdf,
    sha256_of_file,
    ContractDocumentError,
)


def _send_signature_email(sig_req, request):
    """Send the magic-link email to the counterparty."""
    public_url = request.build_absolute_uri(sig_req.public_path())
    sender_name = (
        getattr(sig_req.created_by, 'full_name', None)
        or getattr(sig_req.created_by, 'username', None)
        or 'The team'
    )
    subject = f'Signature requested: {sig_req.contract.title}'
    body_lines = [
        f'Hello {sig_req.signer_name},',
        '',
        f'{sender_name} has requested your signature on the following contract:',
        '',
        f'    {sig_req.contract.title}',
        f'    Reference: {sig_req.contract.reference or "—"}',
        '',
        'You can review and sign the document securely using the link below:',
        '',
        f'    {public_url}',
        '',
        f'This link expires on {sig_req.expires_at.strftime("%B %d, %Y")}.',
    ]
    if sig_req.message:
        body_lines += ['', '── Message from sender ──', sig_req.message]
    body_lines += [
        '',
        'Thank you,',
        sender_name,
    ]
    _send_contract_email(subject, '\n'.join(body_lines), [sig_req.signer_email])


class ContractGenerateDocumentView(LoginRequiredMixin, View):
    """
    POST /finance/contracts/<uuid:pk>/generate-document/

    Renders a Word + PDF version of the contract from its structured fields.
    """

    def dispatch(self, request, *args, **kwargs):
        if not _can_manage_contracts(request.user):
            return HttpResponseForbidden(
                'You do not have permission to generate contract documents.'
            )
        self.contract = get_object_or_404(Contract, pk=kwargs['pk'])
        return super().dispatch(request, *args, **kwargs)

    def post(self, request, pk):
        try:
            doc = generate_contract_document(self.contract, actor=request.user)
        except ContractDocumentError as e:
            messages.error(request, str(e))
            return redirect('contract_detail', pk=self.contract.pk)
        except Exception as e:
            messages.error(request, f'Could not generate document: {e}')
            return redirect('contract_detail', pk=self.contract.pk)

        messages.success(
            request,
            f'Contract document generated (v{doc.version}). '
            'Word and PDF copies are now available.',
        )
        return redirect('contract_detail', pk=self.contract.pk)


class ContractDocumentDownloadView(LoginRequiredMixin, View):
    """
    GET /finance/contracts/<uuid:pk>/documents/<uuid:doc_pk>/download/?fmt=pdf|docx

    Streams the requested file back to the user.
    """

    def get(self, request, pk, doc_pk):
        if not _can_view_contracts(request.user):
            return HttpResponseForbidden('You do not have permission to view contracts.')

        contract = get_object_or_404(Contract, pk=pk)
        doc = get_object_or_404(ContractDocument, pk=doc_pk, contract=contract)

        fmt = (request.GET.get('fmt') or 'pdf').lower()
        if fmt == 'docx':
            f = doc.docx_file
            mime = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
        else:
            f = doc.pdf_file
            mime = 'application/pdf'

        if not f:
            raise Http404(f'No {fmt.upper()} version is available for this document.')

        f.open('rb')
        return FileResponse(f, as_attachment=True, filename=os.path.basename(f.name),
                            content_type=mime)


class ContractSendForSignatureView(LoginRequiredMixin, View):
    """
    POST /finance/contracts/<uuid:pk>/send-for-signature/

    Form fields:
        document_choice  — 'generated' or 'uploaded'
        signer_name      — required
        signer_email     — required
        expires_in_days  — optional integer (default 30)
        message          — optional free-form note
    """

    def dispatch(self, request, *args, **kwargs):
        if not _can_manage_contracts(request.user):
            return HttpResponseForbidden(
                'You do not have permission to send contracts for signature.'
            )
        self.contract = get_object_or_404(Contract, pk=kwargs['pk'])
        return super().dispatch(request, *args, **kwargs)

    def post(self, request, pk):
        c = self.contract
        choice = (request.POST.get('document_choice') or 'generated').strip().lower()

        # ── Block if there's already an open request ───────────────────────
        already_open = c.signature_requests.filter(
            status__in=[
                ContractSignatureRequest.Status.SENT,
                ContractSignatureRequest.Status.VIEWED,
            ]
        ).exists()
        if already_open:
            messages.error(
                request,
                'A signature request is already open on this contract. '
                'Void it first before sending another.',
            )
            return redirect('contract_detail', pk=c.pk)

        # ── Resolve the document to be signed ──────────────────────────────
        if choice == 'uploaded':
            if not c.document:
                messages.error(request, 'No uploaded document is attached to this contract.')
                return redirect('contract_detail', pk=c.pk)
            try:
                doc = register_uploaded_document(c, actor=request.user)
            except ContractDocumentError as e:
                messages.error(request, str(e))
                return redirect('contract_detail', pk=c.pk)
            if not doc.pdf_file:
                messages.error(
                    request,
                    'The uploaded file could not be turned into a signable PDF. '
                    'Generate the document instead, or upload a PDF.',
                )
                return redirect('contract_detail', pk=c.pk)
        else:
            # Generated: use the latest generated document, or generate one now.
            doc = (
                c.documents
                .filter(source=ContractDocument.Source.GENERATED)
                .order_by('-version')
                .first()
            )
            if not doc:
                try:
                    doc = generate_contract_document(c, actor=request.user)
                except ContractDocumentError as e:
                    messages.error(request, str(e))
                    return redirect('contract_detail', pk=c.pk)

        # ── Form fields ────────────────────────────────────────────────────
        signer_name = (request.POST.get('signer_name') or '').strip()
        signer_email = (request.POST.get('signer_email') or '').strip()
        if not signer_name or not signer_email:
            messages.error(request, 'Signer name and email are required.')
            return redirect('contract_detail', pk=c.pk)

        try:
            expires_in_days = int(request.POST.get('expires_in_days') or 30)
        except (TypeError, ValueError):
            expires_in_days = 30

        try:
            sig_req = build_signature_request(
                c, doc,
                signer_name=signer_name,
                signer_email=signer_email,
                message=(request.POST.get('message') or '').strip(),
                expires_in_days=expires_in_days,
                actor=request.user,
            )
        except ContractDocumentError as e:
            messages.error(request, str(e))
            return redirect('contract_detail', pk=c.pk)

        # Mark sent + dispatch email.
        mark_request_sent(sig_req)
        try:
            _send_signature_email(sig_req, request)
        except Exception:
            # Don't fail the request creation if SMTP is misconfigured.
            messages.warning(
                request,
                'Signature request created, but the email could not be sent. '
                'Use "Resend" to try again, or copy the link manually.',
            )

        messages.success(
            request,
            f'Signature request sent to {sig_req.signer_email}. '
            f'Link valid until {sig_req.expires_at.strftime("%b %d, %Y")}.',
        )
        return redirect('contract_detail', pk=c.pk)


class ContractSignatureResendView(LoginRequiredMixin, View):
    """POST /finance/contracts/<uuid:pk>/signature-requests/<uuid:req_pk>/resend/"""

    def dispatch(self, request, *args, **kwargs):
        if not _can_manage_contracts(request.user):
            return HttpResponseForbidden('Permission denied.')
        self.contract = get_object_or_404(Contract, pk=kwargs['pk'])
        self.sig_req = get_object_or_404(
            ContractSignatureRequest, pk=kwargs['req_pk'], contract=self.contract,
        )
        return super().dispatch(request, *args, **kwargs)

    def post(self, request, pk, req_pk):
        if not self.sig_req.is_open:
            messages.error(request, 'This signature request is no longer active.')
            return redirect('contract_detail', pk=self.contract.pk)
        try:
            _send_signature_email(self.sig_req, request)
        except Exception as e:
            messages.error(request, f'Could not send email: {e}')
            return redirect('contract_detail', pk=self.contract.pk)
        # Refresh sent_at so the audit trail reflects the resend.
        self.sig_req.sent_at = timezone.now()
        self.sig_req.save(update_fields=['sent_at', 'updated_at'])
        messages.success(
            request,
            f'Signature reminder sent to {self.sig_req.signer_email}.',
        )
        return redirect('contract_detail', pk=self.contract.pk)


class ContractSignatureVoidView(LoginRequiredMixin, View):
    """POST /finance/contracts/<uuid:pk>/signature-requests/<uuid:req_pk>/void/"""

    def dispatch(self, request, *args, **kwargs):
        if not _can_manage_contracts(request.user):
            return HttpResponseForbidden('Permission denied.')
        self.contract = get_object_or_404(Contract, pk=kwargs['pk'])
        self.sig_req = get_object_or_404(
            ContractSignatureRequest, pk=kwargs['req_pk'], contract=self.contract,
        )
        return super().dispatch(request, *args, **kwargs)

    def post(self, request, pk, req_pk):
        s = self.sig_req
        if s.status == ContractSignatureRequest.Status.SIGNED:
            messages.error(request, 'A completed signature request cannot be voided.')
            return redirect('contract_detail', pk=self.contract.pk)
        s.status = ContractSignatureRequest.Status.VOIDED
        s.voided_at = timezone.now()
        s.voided_by = request.user
        s.save(update_fields=['status', 'voided_at', 'voided_by', 'updated_at'])
        messages.success(request, 'Signature request voided.')
        return redirect('contract_detail', pk=self.contract.pk)


class ContractSignedDownloadView(LoginRequiredMixin, View):
    """GET /finance/contracts/<uuid:pk>/signature-requests/<uuid:req_pk>/signed.pdf"""

    def get(self, request, pk, req_pk):
        if not _can_view_contracts(request.user):
            return HttpResponseForbidden('Permission denied.')
        contract = get_object_or_404(Contract, pk=pk)
        sig_req = get_object_or_404(
            ContractSignatureRequest, pk=req_pk, contract=contract,
        )
        if not sig_req.signed_pdf:
            raise Http404('Signed PDF is not available yet.')
        sig_req.signed_pdf.open('rb')
        return FileResponse(
            sig_req.signed_pdf,
            as_attachment=True,
            filename=os.path.basename(sig_req.signed_pdf.name),
            content_type='application/pdf',
        )


# ── Public signing pages (no login required) ────────────────────────────────

class ContractPublicSignView(View):
    """
    GET  /finance/contracts/sign/<token>/             -> show signing page
    POST /finance/contracts/sign/<token>/             -> submit signature
    """
    template_name = 'finance/contract_public_sign.html'

    def _get_request_or_404(self, token):
        return get_object_or_404(ContractSignatureRequest, access_token=token)

    def _state_context(self, sig_req):
        """Common context — describes whether the link is still actionable."""
        return {
            'sig_req':    sig_req,
            'contract':   sig_req.contract,
            'document':   sig_req.document,
            'is_open':    sig_req.is_open,
            'is_signed':  sig_req.status == ContractSignatureRequest.Status.SIGNED,
            'is_voided':  sig_req.status == ContractSignatureRequest.Status.VOIDED,
            'is_declined':sig_req.status == ContractSignatureRequest.Status.DECLINED,
            'is_expired': sig_req.is_expired,
        }

    def get(self, request, token):
        sig_req = self._get_request_or_404(token)
        # Track viewing.
        if sig_req.is_open:
            mark_request_viewed(sig_req)
        return render(request, self.template_name, self._state_context(sig_req))

    def post(self, request, token):
        sig_req = self._get_request_or_404(token)

        if not sig_req.is_open:
            messages.error(
                request,
                'This signing link is no longer active and cannot be used.',
            )
            return render(request, self.template_name, self._state_context(sig_req))

        action = (request.POST.get('action') or 'sign').strip().lower()

        # ── Decline path ───────────────────────────────────────────────────
        if action == 'decline':
            sig_req.status = ContractSignatureRequest.Status.DECLINED
            sig_req.declined_at = timezone.now()
            sig_req.decline_reason = (request.POST.get('decline_reason') or '').strip()
            sig_req.save(update_fields=['status', 'declined_at', 'decline_reason', 'updated_at'])
            messages.info(request, 'You have declined to sign this document.')
            return render(request, self.template_name, self._state_context(sig_req))

        # ── Sign path ──────────────────────────────────────────────────────
        method_raw = (request.POST.get('method') or 'drawn').strip().lower()
        method_map = {
            'drawn': ContractSignature.Method.DRAWN,
            'typed': ContractSignature.Method.TYPED,
            'upload': ContractSignature.Method.UPLOAD,
        }
        method = method_map.get(method_raw, ContractSignature.Method.DRAWN)

        consent_given = request.POST.get('consent') == 'on'
        if not consent_given:
            messages.error(request, 'You must agree to the electronic signature consent.')
            return render(request, self.template_name, self._state_context(sig_req))

        # Build signature row.
        signature = ContractSignature(
            request=sig_req,
            signer_name_at_signing=sig_req.signer_name,
            signer_email_at_signing=sig_req.signer_email,
            method=method,
            consent_text=(
                'I agree that my electronic signature is the legal equivalent of my '
                'handwritten signature and that this contract may be signed electronically.'
            ),
            consent_given=True,
            signed_ip=_client_ip(request),
            signed_user_agent=request.META.get('HTTP_USER_AGENT', '')[:500],
        )

        try:
            if method == ContractSignature.Method.DRAWN:
                # Expect a base64 data URL from a <canvas>.
                data_url = (request.POST.get('signature_data') or '').strip()
                m = _re.match(r'^data:image/(png|jpe?g);base64,(.+)$', data_url, _re.DOTALL)
                if not m:
                    messages.error(request, 'Please draw your signature before submitting.')
                    return render(request, self.template_name, self._state_context(sig_req))
                ext = 'png' if m.group(1) == 'png' else 'jpg'
                raw = _b64.b64decode(m.group(2))
                signature.signature_image.save(
                    f'sig-{sig_req.id.hex[:8]}.{ext}',
                    ContentFile(raw),
                    save=False,
                )
            elif method == ContractSignature.Method.TYPED:
                typed = (request.POST.get('typed_text') or '').strip()
                if not typed:
                    messages.error(request, 'Please type your name to sign.')
                    return render(request, self.template_name, self._state_context(sig_req))
                signature.typed_text = typed
            elif method == ContractSignature.Method.UPLOAD:
                if 'signature_file' not in request.FILES:
                    messages.error(request, 'Please choose an image of your signature.')
                    return render(request, self.template_name, self._state_context(sig_req))
                f = request.FILES['signature_file']
                signature.signature_image.save(
                    f'sig-{sig_req.id.hex[:8]}-{f.name}',
                    f, save=False,
                )

            # Hash the source PDF at signing time (tamper evidence).
            signature.document_hash_at_signing = sha256_of_file(sig_req.document.pdf_file)
            signature.save()

            # Stamp the signature into a new signed PDF and finalise the request.
            stamp_signature_onto_pdf(sig_req, signature)

        except ContractDocumentError as e:
            messages.error(request, str(e))
            return render(request, self.template_name, self._state_context(sig_req))
        except Exception as e:
            messages.error(request, f'Could not record your signature: {e}')
            return render(request, self.template_name, self._state_context(sig_req))

        # Confirmation email to the signer with a copy of the signed PDF.
        try:
            _send_contract_email(
                subject=f'Signed: {sig_req.contract.title}',
                message=(
                    f'Hello {sig_req.signer_name},\n\n'
                    f'Thank you. Your signature has been recorded for:\n\n'
                    f'    {sig_req.contract.title}\n'
                    f'    Reference: {sig_req.contract.reference or "—"}\n\n'
                    f'A signed copy of the document has been added to the contract record.\n'
                ),
                recipients=[sig_req.signer_email],
            )
        except Exception:
            pass

        return render(request, self.template_name, self._state_context(sig_req))


class ContractPublicSignedDownloadView(View):
    """
    GET /finance/contracts/sign/<token>/signed.pdf

    Lets the counterparty download their own signed copy from the public link.
    """
    def get(self, request, token):
        sig_req = get_object_or_404(ContractSignatureRequest, access_token=token)
        if sig_req.status != ContractSignatureRequest.Status.SIGNED or not sig_req.signed_pdf:
            raise Http404('Signed PDF is not available.')
        sig_req.signed_pdf.open('rb')
        return FileResponse(
            sig_req.signed_pdf,
            as_attachment=True,
            filename=os.path.basename(sig_req.signed_pdf.name),
            content_type='application/pdf',
        )


class ContractPublicSourceDownloadView(View):
    """
    GET /finance/contracts/sign/<token>/document.pdf

    Lets the counterparty preview the source PDF before signing.
    """
    def get(self, request, token):
        sig_req = get_object_or_404(ContractSignatureRequest, access_token=token)
        if not sig_req.is_open and sig_req.status != ContractSignatureRequest.Status.SIGNED:
            raise Http404('Document not available.')
        f = sig_req.document.pdf_file
        if not f:
            raise Http404('Source PDF not available.')
        f.open('rb')
        return FileResponse(
            f, as_attachment=False,
            filename=os.path.basename(f.name),
            content_type='application/pdf',
        )


def _client_ip(request):
    xff = request.META.get('HTTP_X_FORWARDED_FOR', '')
    if xff:
        return xff.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR', '') or None