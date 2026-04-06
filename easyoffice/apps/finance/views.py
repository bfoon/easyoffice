from decimal import Decimal, InvalidOperation
from django.contrib.auth.mixins import LoginRequiredMixin
from django.views.generic import TemplateView, View, DetailView
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.utils import timezone
from django.db.models import Sum, Q
from django.http import HttpResponseForbidden

from apps.finance.models import (
    Budget, PurchaseRequest, Payment,
    EmployeeFinanceRequest, EmployeeLoan, EmployeeLoanPayment,
)


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
        title = (getattr(profile.position, 'title', '') if profile and getattr(profile, 'position', None) else '')
    except Exception:
        title = ''
    return str(title).strip().lower() == 'ceo'


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

def _is_hr(user):
    return user.is_superuser or user.groups.filter(name__in=['HR', 'Human Resources']).exists()


def _can_view_finance_dashboard(user):
    return _is_finance(user) or _is_ceo(user) or _is_hr(user)


def _can_view_my_finance(user):
    return user.is_authenticated

def _validate_decimal(value, field_name='amount'):
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError(f'Please enter a valid {field_name}.')


def _generate_reference(prefix='PAY'):
    return f'{prefix}-{timezone.now().strftime("%Y%m%d%H%M%S%f")}'

def _apply_payment_to_budget(payment):
    """
    Deduct a payment from its linked budget exactly once.
    Safe to call only immediately after creating a new payment.
    """
    budget = getattr(payment, 'budget', None)
    amount = getattr(payment, 'amount', None)

    if not budget or amount is None:
        return

    if amount <= 0:
        return

    new_spent = (budget.spent_amount or Decimal('0')) + amount
    if new_spent > budget.total_amount:
        raise ValueError(f'Payment exceeds available budget for "{budget.name}".')
    budget.spent_amount = new_spent
    budget.save(update_fields=['spent_amount'])


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

        today = timezone.now()
        year = today.year
        month = today.month

        active_budgets = Budget.objects.filter(fiscal_year=year, status='active')
        total_budget = active_budgets.aggregate(t=Sum('total_amount'))['t'] or Decimal('0')
        total_spent = active_budgets.aggregate(t=Sum('spent_amount'))['t'] or Decimal('0')
        total_balance = total_budget - total_spent

        my_requests = PurchaseRequest.objects.filter(requested_by=self.request.user)

        ctx.update({
            'total_budget': total_budget,
            'total_spent': total_spent,
            'total_balance': total_balance,
            'overall_pct': round(float(total_spent) / float(total_budget) * 100, 1) if total_budget else 0,
            'pending_count': PurchaseRequest.objects.filter(status='submitted').count(),
            'approved_count': PurchaseRequest.objects.filter(status='ceo_approved').count(),
            'payments_this_month': Payment.objects.filter(
                payment_date__year=year,
                payment_date__month=month,
            ).aggregate(t=Sum('amount'))['t'] or Decimal('0'),
            'budgets': active_budgets.select_related('department', 'unit').order_by('-created_at')[:8],
            'pending_purchases': PurchaseRequest.objects.filter(
                status='submitted'
            ).select_related(
                'requested_by', 'department', 'budget', 'project'
            ).order_by('-created_at')[:10],
            'pending_staff_requests': EmployeeFinanceRequest.objects.filter(
                status='submitted'
            ).select_related(
                'employee', 'budget', 'project'
            ).order_by('-created_at')[:10],
            'finance_processing_queue': EmployeeFinanceRequest.objects.filter(
                status='ceo_approved'
            ).select_related(
                'employee', 'budget', 'project'
            ).order_by('-approval_date', '-created_at')[:10],
            'recent_payments': Payment.objects.select_related(
                'paid_by', 'purchase_request', 'budget', 'employee', 'project'
            ).order_by('-payment_date', '-created_at')[:8],
            'my_recent_requests': my_requests.select_related(
                'department', 'budget', 'project'
            ).order_by('-created_at')[:6],
            'my_pending_requests': my_requests.filter(status='submitted').count(),
            'my_draft_requests': my_requests.filter(status='draft').count(),

            'is_finance': _is_finance(self.request.user),
            'is_ceo': _is_ceo(self.request.user),
            'is_hr': _is_hr(self.request.user),
            'can_view_finance_dashboard': _can_view_finance_dashboard(self.request.user),
            'can_view_my_finance': _can_view_my_finance(self.request.user),
        })
        return ctx


# ── Staff My Finance Dashboard ──────────────────────────────────────────────

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
            'my_requests': EmployeeFinanceRequest.objects.filter(employee=user).order_by('-created_at')[:12],
            'my_loans': my_loans,
            'active_loan': active_loan,
            'paid_this_month': my_payments.filter(
                direction=Payment.Direction.COMPANY_TO_STAFF,
                payment_date__year=year,
                payment_date__month=month
            ).aggregate(t=Sum('amount'))['t'] or Decimal('0'),
            'paid_ytd': my_payments.filter(
                direction=Payment.Direction.COMPANY_TO_STAFF,
                payment_date__year=year
            ).aggregate(t=Sum('amount'))['t'] or Decimal('0'),
            'deductions_ytd': my_payments.filter(
                direction=Payment.Direction.STAFF_TO_COMPANY,
                payment_date__year=year
            ).aggregate(t=Sum('amount'))['t'] or Decimal('0'),
            'salary_ytd': my_payments.filter(
                payment_type=Payment.PaymentType.SALARY,
                payment_date__year=year
            ).aggregate(t=Sum('amount'))['t'] or Decimal('0'),
            'allowance_ytd': my_payments.filter(
                payment_type=Payment.PaymentType.ALLOWANCE,
                payment_date__year=year
            ).aggregate(t=Sum('amount'))['t'] or Decimal('0'),
            'overtime_ytd': my_payments.filter(
                payment_type=Payment.PaymentType.OVERTIME,
                payment_date__year=year
            ).aggregate(t=Sum('amount'))['t'] or Decimal('0'),
            'transport_ytd': my_payments.filter(
                payment_type=Payment.PaymentType.TRANSPORT_REFUND,
                payment_date__year=year
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
        if not _can_view_finance_dashboard(request.user):
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
                status=PurchaseRequest.Status.DRAFT
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

        status_value = PurchaseRequest.Status.DRAFT if save_as == 'draft' else PurchaseRequest.Status.SUBMITTED

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
            f'Draft "{pr.title}" saved.' if status_value == PurchaseRequest.Status.DRAFT
            else f'Purchase request "{pr.title}" submitted for CEO approval.'
        )
        return redirect('purchase_request')


class PurchaseRequestDetailView(LoginRequiredMixin, DetailView):
    model = PurchaseRequest
    template_name = 'finance/purchase_detail.html'
    context_object_name = 'purchase'

    def dispatch(self, request, *args, **kwargs):
        purchase = self.get_object()
        if not _can_view_purchase(request.user, purchase):
            return HttpResponseForbidden('You do not have permission to view this purchase request.')
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        purchase = self.object
        ctx['is_finance'] = _is_finance(self.request.user)
        ctx['is_ceo'] = _is_ceo(self.request.user)
        ctx['can_edit_purchase'] = _can_edit_purchase(self.request.user, purchase)
        ctx['payments'] = purchase.payments.select_related('paid_by', 'budget', 'employee').order_by('-payment_date', '-created_at')
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
            'my_requests': EmployeeFinanceRequest.objects.filter(employee=request.user).select_related(
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
                status=EmployeeFinanceRequest.Status.DRAFT
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

        status = EmployeeFinanceRequest.Status.DRAFT if action == 'draft' else EmployeeFinanceRequest.Status.SUBMITTED

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
            f'Draft "{req.title}" saved.' if status == EmployeeFinanceRequest.Status.DRAFT
            else f'"{req.title}" submitted for CEO approval.'
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
            'to_process': EmployeeFinanceRequest.objects.filter(status='ceo_approved').select_related(
                'employee', 'budget', 'project', 'approved_by'
            ).order_by('-approval_date', '-created_at'),
            'processed': EmployeeFinanceRequest.objects.filter(status__in=['processing', 'paid', 'closed']).select_related(
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
            recipient=req.employee.get_full_name() if hasattr(req.employee, 'get_full_name') else str(req.employee),
            payment_date=request.POST.get('payment_date') or timezone.now().date(),
            budget=req.budget,
            project=req.project,
            notes=request.POST.get('notes', ''),
        )

        if 'receipt' in request.FILES:
            payment.receipt = request.FILES['receipt']
            payment.save(update_fields=['receipt'])

        # ✅ deduct from budget
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
        loans = EmployeeLoan.objects.filter(employee=self.request.user).prefetch_related(
            'repayment_rows', 'payments'
        ).order_by('-created_at')
        ctx.update({
            'loans': loans,
        })
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
                Q(reference__icontains=q) |
                Q(description__icontains=q) |
                Q(recipient__icontains=q)
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