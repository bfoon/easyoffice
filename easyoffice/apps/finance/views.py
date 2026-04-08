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
            'recent_payment_requests': PaymentRequest.objects.select_related(
                'requested_by', 'recipient_user',
            ).order_by('-created_at')[:5],
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


class PurchaseRequestDetailView(LoginRequiredMixin, TemplateView):
    template_name = 'finance/purchase_detail.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)

        purchase = get_object_or_404(PurchaseRequest, pk=self.kwargs['pk'])

        ctx.update({
            'purchase': purchase,
            'payments': Payment.objects.filter(purchase_request=purchase).order_by('-payment_date'),

            # ✅ FIX: pass choices here
            'status_choices': PurchaseRequest.Status.choices,

            'is_ceo': _is_ceo(self.request.user),
            'is_finance': _is_finance(self.request.user),
            'can_edit_purchase': purchase.requested_by == self.request.user and purchase.status in ['draft', 'rejected'],
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

# ─────────────────────────────────────────────────────────────────────────────
# Payment Request — helpers & email templates
# ─────────────────────────────────────────────────────────────────────────────

from apps.finance.models import PaymentRequest, PaymentRequestDocument


def _can_manage_payment_requests(user):
    return _is_finance(user) or _is_ceo(user) or user.is_superuser


def _send_payment_request_email(payment_request, request=None):
    """Send a professional notification email to the recipient when a payment request is created/sent."""
    from django.core.mail import EmailMessage
    from django.conf import settings

    recipient_email = payment_request.effective_recipient_email
    if not recipient_email:
        return

    office_name = getattr(settings, 'OFFICE_NAME', 'EasyOffice')
    org_name    = getattr(settings, 'ORGANISATION_NAME', office_name)
    currency    = payment_request.currency
    amount      = f'{payment_request.amount:,.2f}'
    pay_method  = payment_request.get_method_display()
    due_date    = payment_request.due_date.strftime('%d %B %Y') if payment_request.due_date else 'As soon as possible'
    requester   = getattr(payment_request.requested_by, 'full_name', str(payment_request.requested_by))

    html = f"""<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<style>
  body {{ margin:0;padding:0;background:#f1f5f9;font-family:'Segoe UI',Arial,sans-serif; }}
  .wrapper {{ max-width:620px;margin:32px auto;background:#fff;border-radius:16px;overflow:hidden;box-shadow:0 4px 24px rgba(0,0,0,.08); }}
  .header {{ background:linear-gradient(135deg,#1e3a5f 0%,#2563eb 100%);padding:36px 40px 32px;text-align:center; }}
  .header h1 {{ margin:0;font-size:22px;color:#fff;font-weight:700;letter-spacing:-.3px; }}
  .header p  {{ margin:6px 0 0;font-size:13px;color:rgba(255,255,255,.75); }}
  .badge {{ display:inline-block;background:rgba(255,255,255,.2);color:#fff;border-radius:20px;padding:4px 14px;font-size:12px;font-weight:700;margin-top:12px;letter-spacing:.5px; }}
  .body {{ padding:36px 40px; }}
  .greeting {{ font-size:16px;color:#1e293b;margin-bottom:20px;line-height:1.6; }}
  .amount-box {{ background:linear-gradient(135deg,#eff6ff,#dbeafe);border:1px solid #bfdbfe;border-radius:12px;padding:20px 24px;text-align:center;margin:24px 0; }}
  .amount-box .label {{ font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:.06em;color:#1d4ed8;margin-bottom:6px; }}
  .amount-box .value {{ font-size:32px;font-weight:800;color:#1e3a5f; }}
  .detail-table {{ width:100%;border-collapse:collapse;margin:24px 0; }}
  .detail-table td {{ padding:11px 14px;font-size:14px; }}
  .detail-table tr:nth-child(odd) td {{ background:#f8fafc; }}
  .detail-table td:first-child {{ font-weight:700;color:#475569;width:38%;border-radius:6px 0 0 6px; }}
  .detail-table td:last-child {{ color:#1e293b;border-radius:0 6px 6px 0; }}
  .divider {{ height:1px;background:#e2e8f0;margin:28px 0; }}
  .footer {{ background:#f8fafc;padding:24px 40px;text-align:center;border-top:1px solid #e2e8f0; }}
  .footer p {{ margin:0;font-size:12px;color:#94a3b8;line-height:1.7; }}
  .footer strong {{ color:#64748b; }}
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
    <p class="greeting">
      Dear <strong>{payment_request.effective_recipient_name}</strong>,<br><br>
      We are pleased to inform you that a payment has been initiated on your behalf by the Finance Department of <strong>{org_name}</strong>.
      Please find the details below.
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

    {'<div style="background:#fffbeb;border:1px solid #fde68a;border-radius:10px;padding:14px 18px;font-size:14px;color:#92400e;margin:20px 0"><strong>Description:</strong><br>' + payment_request.description + '</div>' if payment_request.description else ''}

    {'<div style="background:#f0fdf4;border:1px solid #86efac;border-radius:10px;padding:14px 18px;font-size:14px;color:#14532d;margin:20px 0"><strong>Payment Details / Bank Information:</strong><br>' + payment_request.recipient_notes + '</div>' if payment_request.recipient_notes else ''}

    <div class="divider"></div>
    <p style="font-size:13px;color:#64748b;line-height:1.7">
      If you have any questions regarding this payment, please contact the Finance Department directly.
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
        from_email=getattr(settings, 'DEFAULT_FROM_EMAIL', f'finance@{org_name.lower().replace(" ","")}.org'),
        to=[recipient_email],
    )
    msg.content_subtype = 'html'
    try:
        msg.send()
    except Exception:
        pass


def _send_payment_confirmation_email(payment_request):
    """Send a professional thank-you / payment acknowledgement email after payment is marked paid."""
    from django.core.mail import EmailMessage
    from django.conf import settings

    recipient_email = payment_request.effective_recipient_email
    if not recipient_email:
        return

    office_name = getattr(settings, 'OFFICE_NAME', 'EasyOffice')
    org_name    = getattr(settings, 'ORGANISATION_NAME', office_name)
    currency    = payment_request.currency
    amount      = f'{payment_request.amount:,.2f}'
    pay_method  = payment_request.get_method_display()
    paid_at     = payment_request.paid_at.strftime('%d %B %Y at %H:%M') if payment_request.paid_at else 'Today'
    ref         = payment_request.linked_payment.reference if payment_request.linked_payment else str(payment_request.id)

    html = f"""<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<style>
  body {{ margin:0;padding:0;background:#f1f5f9;font-family:'Segoe UI',Arial,sans-serif; }}
  .wrapper {{ max-width:620px;margin:32px auto;background:#fff;border-radius:16px;overflow:hidden;box-shadow:0 4px 24px rgba(0,0,0,.08); }}
  .header {{ background:linear-gradient(135deg,#064e3b 0%,#10b981 100%);padding:36px 40px 32px;text-align:center; }}
  .header h1 {{ margin:0;font-size:22px;color:#fff;font-weight:700;letter-spacing:-.3px; }}
  .header p  {{ margin:6px 0 0;font-size:13px;color:rgba(255,255,255,.75); }}
  .checkmark {{ font-size:48px;margin-bottom:8px;display:block; }}
  .body {{ padding:36px 40px; }}
  .greeting {{ font-size:16px;color:#1e293b;margin-bottom:20px;line-height:1.6; }}
  .amount-box {{ background:linear-gradient(135deg,#ecfdf5,#d1fae5);border:1px solid #6ee7b7;border-radius:12px;padding:20px 24px;text-align:center;margin:24px 0; }}
  .amount-box .label {{ font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:.06em;color:#065f46;margin-bottom:6px; }}
  .amount-box .value {{ font-size:32px;font-weight:800;color:#064e3b; }}
  .detail-table {{ width:100%;border-collapse:collapse;margin:24px 0; }}
  .detail-table td {{ padding:11px 14px;font-size:14px; }}
  .detail-table tr:nth-child(odd) td {{ background:#f8fafc; }}
  .detail-table td:first-child {{ font-weight:700;color:#475569;width:38%;border-radius:6px 0 0 6px; }}
  .detail-table td:last-child {{ color:#1e293b;border-radius:0 6px 6px 0; }}
  .success-bar {{ background:#d1fae5;border-radius:10px;padding:14px 18px;text-align:center;font-size:14px;font-weight:700;color:#065f46;margin:20px 0;border:1px solid #6ee7b7; }}
  .divider {{ height:1px;background:#e2e8f0;margin:28px 0; }}
  .footer {{ background:#f8fafc;padding:24px 40px;text-align:center;border-top:1px solid #e2e8f0; }}
  .footer p {{ margin:0;font-size:12px;color:#94a3b8;line-height:1.7; }}
  .footer strong {{ color:#64748b; }}
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
    <p class="greeting">
      Dear <strong>{payment_request.effective_recipient_name}</strong>,<br><br>
      We are pleased to confirm that the following payment has been <strong>successfully processed</strong> by {org_name}.
      Please retain this confirmation for your records.
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

    <div class="divider"></div>
    <p style="font-size:14px;color:#1e293b;line-height:1.8">
      Thank you for your continued engagement with <strong>{org_name}</strong>.
      If you believe this payment was made in error, or if you have any questions,
      please contact the Finance Department immediately quoting the reference above.
    </p>
    <p style="font-size:13px;color:#64748b;line-height:1.7">
      Please do not reply to this automated email.
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
        from_email=getattr(settings, 'DEFAULT_FROM_EMAIL', f'finance@{org_name.lower().replace(" ","")}.org'),
        to=[recipient_email],
    )
    msg.content_subtype = 'html'
    try:
        msg.send()
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# Payment Request Views
# ─────────────────────────────────────────────────────────────────────────────

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
                Q(title__icontains=q) |
                Q(recipient_name__icontains=q) |
                Q(recipient_email__icontains=q) |
                Q(description__icontains=q)
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
        return {
            'mode': 'create',
            'staff_list':       CoreUser.objects.filter(is_active=True, status='active').order_by('first_name'),
            'budgets':          Budget.objects.filter(status__in=['approved','active']).order_by('name'),
            'projects':         __import__('apps.projects.models', fromlist=['Project']).Project.objects.filter(status='active').order_by('name'),
            'purchase_requests': PurchaseRequest.objects.exclude(status__in=['closed','rejected']).order_by('-created_at')[:30],
            'payment_types':    Payment.PaymentType.choices,
            'method_choices':   Payment.Method.choices,
            'rtype_choices':    PaymentRequest.RecipientType.choices,
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
                    recipient_name  = getattr(recipient_user, 'full_name', str(recipient_user))
                    recipient_email = recipient_email or recipient_user.email

        pr = PaymentRequest.objects.create(
            title           = request.POST.get('title', '').strip(),
            description     = request.POST.get('description', '').strip(),
            payment_type    = request.POST.get('payment_type', Payment.PaymentType.OTHER),
            recipient_type  = rtype,
            recipient_user  = recipient_user,
            recipient_name  = recipient_name,
            recipient_email = recipient_email,
            recipient_notes = request.POST.get('recipient_notes', '').strip(),
            amount          = amount,
            currency        = request.POST.get('currency', 'GMD').strip().upper() or 'GMD',
            method          = request.POST.get('method', Payment.Method.BANK_TRANSFER),
            due_date        = request.POST.get('due_date') or None,
            budget_id       = request.POST.get('budget') or None,
            project_id      = request.POST.get('project') or None,
            purchase_request_id = request.POST.get('purchase_request') or None,
            status          = request.POST.get('status', PaymentRequest.Status.DRAFT),
            requested_by    = request.user,
            notes           = request.POST.get('notes', '').strip(),
        )

        # ── Upload documents ──────────────────────────────────────────────
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

        # ── Send email if status = sent ───────────────────────────────────
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
        ctx = {
            'pr': pr,
            'documents': pr.documents.all(),
            'method_choices': Payment.Method.choices,
            'is_finance': _is_finance(request.user),
            'is_ceo': _is_ceo(request.user),
        }
        return render(request, self.template_name, ctx)


class PaymentRequestMarkPaidView(LoginRequiredMixin, View):
    """Mark a payment request as paid, create the Payment record, send confirmation email."""

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
            amount = _validate_decimal(
                request.POST.get('amount') or pr.amount, 'amount')
        except ValueError as e:
            messages.error(request, str(e))
            return redirect('payment_request_detail', pk=pk)

        payment_date = request.POST.get('payment_date') or timezone.now().date()
        method       = request.POST.get('method') or pr.method
        notes        = request.POST.get('notes', '').strip()
        currency     = request.POST.get('currency') or pr.currency

        # Determine direction
        if pr.recipient_type == PaymentRequest.RecipientType.STAFF:
            direction = Payment.Direction.COMPANY_TO_STAFF
        elif pr.recipient_type == PaymentRequest.RecipientType.VENDOR:
            direction = Payment.Direction.COMPANY_TO_VENDOR
        else:
            direction = Payment.Direction.COMPANY_TO_VENDOR

        payment = Payment.objects.create(
            reference       = _generate_reference('PREQ'),
            description     = pr.title,
            purchase_request= pr.purchase_request,
            amount          = amount,
            currency        = currency,
            method          = method,
            direction       = direction,
            payment_type    = pr.payment_type,
            paid_by         = request.user,
            processed_by    = request.user,
            approved_by     = pr.approved_by or request.user,
            employee        = pr.recipient_user,
            recipient       = pr.effective_recipient_name,
            payment_date    = payment_date,
            budget          = pr.budget,
            project         = pr.project,
            notes           = notes,
        )

        if 'receipt' in request.FILES:
            payment.receipt = request.FILES['receipt']
            payment.save(update_fields=['receipt'])

        # Attach any additional docs to the payment request
        for key in request.FILES:
            if key.startswith('doc_file_'):
                idx   = key[len('doc_file_'):]
                f     = request.FILES[key]
                dtype = request.POST.get(f'doc_type_{idx}', PaymentRequestDocument.DocType.RECEIPT)
                dname = request.POST.get(f'doc_name_{idx}', f.name).strip() or f.name
                doc   = PaymentRequestDocument(
                    payment_request=pr,
                    doc_type=dtype,
                    name=dname,
                    uploaded_by=request.user,
                )
                doc.file.save(f.name, f, save=True)

        _apply_payment_to_budget(payment)

        pr.status         = PaymentRequest.Status.PAID
        pr.linked_payment = payment
        pr.paid_at        = timezone.now()
        pr.approved_by    = pr.approved_by or request.user
        pr.save(update_fields=['status', 'linked_payment', 'paid_at', 'approved_by', 'updated_at'])

        # Send confirmation email to recipient
        _send_payment_confirmation_email(pr)

        messages.success(request, f'"{pr.title}" marked as paid. Confirmation email sent to {pr.effective_recipient_email or "recipient"}.')
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

        messages.success(request, f'Payment request sent and email dispatched to {pr.effective_recipient_email or pr.effective_recipient_name}.')
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


# ─────────────────────────────────────────────────────────────────────────────
# Incoming Payment Request (Company → Customer invoicing)
# ─────────────────────────────────────────────────────────────────────────────

from apps.finance.models import IncomingPaymentRequest, IncomingPaymentDocument


def _send_invoice_email(ipr, request=None):
    """
    Send a professional invoice email to the customer.
    Attaches all documents uploaded to the request.
    """
    from django.core.mail import EmailMessage
    from django.conf import settings

    if not ipr.customer_email:
        return

    office_name = getattr(settings, 'OFFICE_NAME', 'EasyOffice')
    org_name    = getattr(settings, 'ORGANISATION_NAME', office_name)
    org_email   = getattr(settings, 'DEFAULT_FROM_EMAIL',
                          f'finance@{org_name.lower().replace(" ","")}.org')
    org_address = getattr(settings, 'ORGANISATION_ADDRESS', '')
    org_phone   = getattr(settings, 'ORGANISATION_PHONE', '')

    currency    = ipr.currency
    subtotal    = f'{ipr.amount:,.2f}'
    tax         = f'{ipr.tax_amount:,.2f}'
    discount    = f'{ipr.discount_amount:,.2f}'
    total       = f'{ipr.total_amount:,.2f}'
    due_date    = ipr.due_date.strftime('%d %B %Y') if ipr.due_date else 'Upon receipt'
    issue_date  = ipr.issue_date.strftime('%d %B %Y')

    tax_row = ''
    if ipr.tax_amount:
        tax_row = f'<tr><td style="padding:8px 0;color:#64748b">Tax / VAT</td><td style="text-align:right;padding:8px 0;color:#64748b">{currency} {tax}</td></tr>'
    disc_row = ''
    if ipr.discount_amount:
        disc_row = f'<tr><td style="padding:8px 0;color:#64748b">Discount</td><td style="text-align:right;padding:8px 0;color:#ef4444">- {currency} {discount}</td></tr>'

    customer_block = ipr.customer_name
    if ipr.customer_company and ipr.customer_company != ipr.customer_name:
        customer_block = f'{ipr.customer_name}<br>{ipr.customer_company}'
    if ipr.customer_address:
        customer_block += f'<br><span style="color:#94a3b8;font-size:13px">{ipr.customer_address.replace(chr(10),"<br>")}</span>'

    org_footer_block = org_name
    if org_address:
        org_footer_block += f'<br>{org_address}'
    if org_phone:
        org_footer_block += f'<br>{org_phone}'

    pay_inst_block = ''
    if ipr.payment_instructions:
        pay_inst_block = f"""
<div style="background:#f0fdf4;border:1px solid #86efac;border-radius:12px;padding:20px 24px;margin:24px 0">
  <div style="font-size:13px;font-weight:700;text-transform:uppercase;letter-spacing:.06em;color:#065f46;margin-bottom:10px">
    Payment Instructions
  </div>
  <div style="font-size:14px;color:#1e293b;line-height:1.8;white-space:pre-line">{ipr.payment_instructions}</div>
</div>"""

    html = f"""<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<style>
  body {{ margin:0;padding:0;background:#f1f5f9;font-family:'Segoe UI',Arial,sans-serif; }}
  .wrapper {{ max-width:660px;margin:32px auto;background:#fff;border-radius:16px;overflow:hidden;box-shadow:0 4px 24px rgba(0,0,0,.08); }}
  .header {{ background:linear-gradient(135deg,#1e3a5f 0%,#1d4ed8 100%);padding:0; }}
  .header-inner {{ padding:36px 44px 28px; }}
  .header h1 {{ margin:0 0 4px;font-size:28px;color:#fff;font-weight:900;letter-spacing:-.5px; }}
  .header-sub {{ font-size:13px;color:rgba(255,255,255,.7); }}
  .inv-bar {{ background:rgba(255,255,255,.12);padding:14px 44px;display:flex;justify-content:space-between;font-size:13px; }}
  .inv-bar .lbl {{ color:rgba(255,255,255,.65);font-weight:600;text-transform:uppercase;letter-spacing:.05em;font-size:11px; }}
  .inv-bar .val {{ color:#fff;font-weight:700;margin-top:3px; }}
  .body {{ padding:36px 44px; }}
  .bill-grid {{ display:grid;grid-template-columns:1fr 1fr;gap:24px;margin-bottom:32px; }}
  .bill-box .lbl {{ font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.06em;color:#94a3b8;margin-bottom:8px; }}
  .bill-box .val {{ font-size:14px;color:#1e293b;line-height:1.7; }}
  .bill-box .org {{ font-weight:700;font-size:16px;color:#1e3a5f; }}
  .desc-box {{ background:#f8fafc;border-radius:10px;padding:18px 20px;margin-bottom:28px;font-size:14px;color:#475569;line-height:1.7; }}
  .totals {{ border-top:2px solid #e2e8f0;padding-top:16px;margin-top:4px; }}
  .totals table {{ width:100%;border-collapse:collapse; }}
  .total-row td {{ font-size:16px;font-weight:800;color:#1e3a5f;padding:12px 0 0;border-top:2px solid #e2e8f0; }}
  .due-banner {{ background:linear-gradient(135deg,#fffbeb,#fef3c7);border:1px solid #fde68a;border-radius:12px;padding:16px 20px;margin:24px 0;display:flex;align-items:center;gap:14px; }}
  .due-banner .icon {{ font-size:24px; }}
  .due-banner .text {{ font-size:14px;color:#92400e; }}
  .due-banner .amount {{ font-size:22px;font-weight:900;color:#78350f; }}
  .attach-note {{ font-size:13px;color:#64748b;margin-top:8px;font-style:italic; }}
  .footer {{ background:#f8fafc;padding:24px 44px;border-top:1px solid #e2e8f0;text-align:center; }}
  .footer p {{ margin:0;font-size:12px;color:#94a3b8;line-height:1.8; }}
</style>
</head>
<body>
<div class="wrapper">
  <div class="header">
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
  </div>

  <div class="body">
    <div class="bill-grid">
      <div class="bill-box">
        <div class="lbl">From</div>
        <div class="val"><span class="org">{org_name}</span><br>
          <span style="color:#64748b;font-size:13px">{org_address.replace(chr(10),'<br>') if org_address else ''}</span>
        </div>
      </div>
      <div class="bill-box">
        <div class="lbl">Bill To</div>
        <div class="val"><span class="org">{customer_block}</span></div>
      </div>
    </div>

    <div style="font-size:13px;font-weight:700;text-transform:uppercase;letter-spacing:.06em;color:#94a3b8;margin-bottom:8px">Description</div>
    <div class="desc-box">
      <strong style="color:#1e293b">{ipr.title}</strong>
      {'<br><br>' + ipr.description.replace(chr(10),'<br>') if ipr.description else ''}
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
      <div class="icon">💳</div>
      <div>
        <div class="text">Amount due by <strong>{due_date}</strong></div>
        <div class="amount">{currency} {total}</div>
      </div>
    </div>

    {pay_inst_block}

    {'<div class="attach-note">📎 Supporting documents (invoice, delivery note, etc.) are attached to this email.</div>' if ipr.documents.exists() else ''}
  </div>

  <div class="footer">
    <p><strong>{org_name}</strong><br>
    {org_footer_block}<br>
    This is an official invoice. Please quote <strong>{ipr.invoice_number}</strong> in all correspondence.</p>
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

    # ── Attach documents ──────────────────────────────────────────────────
    for doc in ipr.documents.all():
        try:
            doc.file.open('rb')
            msg.attach(doc.name, doc.file.read(),
                       'application/octet-stream')
            doc.file.close()
        except Exception:
            pass

    try:
        msg.send()
    except Exception:
        pass


def _send_invoice_reminder_email(ipr):
    """
    Send a polite but firm payment reminder email to the customer.
    Escalates in tone based on reminder_count.
    """
    from django.core.mail import EmailMessage
    from django.conf import settings

    if not ipr.customer_email:
        return

    office_name = getattr(settings, 'OFFICE_NAME', 'EasyOffice')
    org_name    = getattr(settings, 'ORGANISATION_NAME', office_name)
    org_email   = getattr(settings, 'DEFAULT_FROM_EMAIL',
                          f'finance@{org_name.lower().replace(" ","")}.org')

    currency   = ipr.currency
    total      = f'{ipr.total_amount:,.2f}'
    due_date   = ipr.due_date.strftime('%d %B %Y') if ipr.due_date else 'as soon as possible'
    days_late  = ipr.days_overdue
    count      = ipr.reminder_count + 1  # upcoming count

    # Escalate tone: 1st gentle, 2nd firm, 3rd urgent
    if count == 1:
        tone_subject  = f'Friendly Reminder — Invoice {ipr.invoice_number} Due'
        tone_heading  = 'Friendly Payment Reminder'
        tone_color    = '#3b82f6'
        tone_bg       = 'linear-gradient(135deg,#eff6ff,#dbeafe)'
        tone_border   = '#bfdbfe'
        tone_icon     = '📋'
        tone_body     = (
            f'We hope this message finds you well. This is a friendly reminder that invoice '
            f'<strong>{ipr.invoice_number}</strong> for <strong>{currency} {total}</strong> '
            f'was due on <strong>{due_date}</strong> and has not yet been received.'
        )
        tone_closing  = 'If you have already made this payment, please disregard this message and accept our thanks.'
    elif count == 2:
        tone_subject  = f'Second Notice — Payment Overdue: Invoice {ipr.invoice_number}'
        tone_heading  = 'Payment Overdue — Second Notice'
        tone_color    = '#f59e0b'
        tone_bg       = 'linear-gradient(135deg,#fffbeb,#fef3c7)'
        tone_border   = '#fde68a'
        tone_icon     = '⚠️'
        tone_body     = (
            f'Our records show that invoice <strong>{ipr.invoice_number}</strong> for '
            f'<strong>{currency} {total}</strong>, due on <strong>{due_date}</strong>, '
            f'remains unpaid ({days_late} days overdue). We kindly request that you arrange '
            f'payment at your earliest convenience.'
        )
        tone_closing  = 'Please contact us immediately if there is a dispute or if you require assistance.'
    else:
        tone_subject  = f'URGENT — Final Notice: Invoice {ipr.invoice_number} Seriously Overdue'
        tone_heading  = 'Final Payment Notice'
        tone_color    = '#ef4444'
        tone_bg       = 'linear-gradient(135deg,#fef2f2,#fee2e2)'
        tone_border   = '#fca5a5'
        tone_icon     = '🔴'
        tone_body     = (
            f'Despite previous reminders, invoice <strong>{ipr.invoice_number}</strong> for '
            f'<strong>{currency} {total}</strong> remains unpaid. This account is now '
            f'<strong>{days_late} days overdue</strong>. Immediate payment is required. '
            f'Failure to settle this invoice may result in further action.'
        )
        tone_closing  = 'Please contact us urgently to resolve this matter.'

    pay_inst_block = ''
    if ipr.payment_instructions:
        pay_inst_block = f"""
<div style="background:#f0fdf4;border:1px solid #86efac;border-radius:12px;padding:18px 22px;margin:24px 0">
  <div style="font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:.06em;color:#065f46;margin-bottom:8px">Payment Instructions</div>
  <div style="font-size:14px;color:#1e293b;line-height:1.8;white-space:pre-line">{ipr.payment_instructions}</div>
</div>"""

    html = f"""<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<style>
  body {{ margin:0;padding:0;background:#f1f5f9;font-family:'Segoe UI',Arial,sans-serif; }}
  .wrapper {{ max-width:620px;margin:32px auto;background:#fff;border-radius:16px;overflow:hidden;box-shadow:0 4px 24px rgba(0,0,0,.08); }}
  .header {{ background:{tone_color};padding:32px 40px;text-align:center; }}
  .header .icon {{ font-size:40px;margin-bottom:10px;display:block; }}
  .header h1 {{ margin:0;font-size:22px;color:#fff;font-weight:800; }}
  .header p {{ margin:6px 0 0;font-size:13px;color:rgba(255,255,255,.8); }}
  .body {{ padding:36px 40px; }}
  .summary-box {{ background:{tone_bg};border:1px solid {tone_border};border-radius:12px;padding:20px 24px;margin:20px 0;text-align:center; }}
  .summary-box .inv {{ font-size:13px;color:#64748b;margin-bottom:6px; }}
  .summary-box .amount {{ font-size:32px;font-weight:900;color:#1e293b; }}
  .summary-box .due {{ font-size:13px;color:{tone_color};font-weight:700;margin-top:4px; }}
  .detail-table {{ width:100%;border-collapse:collapse;margin:20px 0; }}
  .detail-table td {{ padding:10px 14px;font-size:14px; }}
  .detail-table tr:nth-child(odd) td {{ background:#f8fafc; }}
  .detail-table td:first-child {{ font-weight:700;color:#475569;width:40%; }}
  .footer {{ background:#f8fafc;padding:22px 40px;border-top:1px solid #e2e8f0;text-align:center; }}
  .footer p {{ margin:0;font-size:12px;color:#94a3b8;line-height:1.7; }}
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
    <p style="font-size:15px;color:#1e293b;line-height:1.7;margin-bottom:16px">
      Dear <strong>{ipr.customer_name}</strong>,
    </p>
    <p style="font-size:14px;color:#475569;line-height:1.8">{tone_body}</p>

    <div class="summary-box">
      <div class="inv">Invoice {ipr.invoice_number} · {ipr.title}</div>
      <div class="amount">{currency} {total}</div>
      <div class="due">{'⚠ ' + str(days_late) + ' days overdue' if days_late else 'Due: ' + due_date}</div>
    </div>

    <table class="detail-table">
      <tr><td>Invoice Number</td><td><strong>{ipr.invoice_number}</strong></td></tr>
      <tr><td>Amount Due</td><td><strong>{currency} {total}</strong></td></tr>
      <tr><td>Original Due Date</td><td>{due_date}</td></tr>
      {'<tr><td>Days Overdue</td><td style="color:#ef4444;font-weight:700">' + str(days_late) + ' days</td></tr>' if days_late else ''}
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

    # Re-attach documents with reminder
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


# ─────────────────────────────────────────────────────────────────────────────
# Incoming Payment Request Views
# ─────────────────────────────────────────────────────────────────────────────

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
                Q(invoice_number__icontains=q) |
                Q(customer_name__icontains=q)  |
                Q(customer_email__icontains=q) |
                Q(customer_company__icontains=q)|
                Q(title__icontains=q)
            )
        if status:
            qs = qs.filter(status=status)

        # Auto-mark overdue
        for ipr in qs:
            if ipr.is_overdue and ipr.status == IncomingPaymentRequest.Status.SENT:
                ipr.status = IncomingPaymentRequest.Status.OVERDUE
                ipr.save(update_fields=['status', 'updated_at'])

        total_outstanding = qs.exclude(
            status__in=['paid', 'cancelled']
        ).aggregate(t=Sum('amount'))['t'] or Decimal('0')

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
        return {
            'mode':    'create',
            'budgets': Budget.objects.filter(status__in=['approved','active']).order_by('name'),
            'projects': __import__('apps.projects.models', fromlist=['Project']).Project.objects.filter(
                status='active').order_by('name'),
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

        # Attach documents
        for key in request.FILES:
            if key.startswith('doc_file_'):
                idx   = key[len('doc_file_'):]
                f     = request.FILES[key]
                dtype = request.POST.get(f'doc_type_{idx}',
                                         IncomingPaymentDocument.DocType.INVOICE)
                dname = request.POST.get(f'doc_name_{idx}', f.name).strip() or f.name
                doc   = IncomingPaymentDocument(
                    payment_request=ipr, doc_type=dtype,
                    name=dname, uploaded_by=request.user,
                )
                doc.file.save(f.name, f, save=True)

        if ipr.status == IncomingPaymentRequest.Status.SENT:
            _send_invoice_email(ipr, request)
            messages.success(request, f'Invoice {ipr.invoice_number} created and emailed to {ipr.customer_email}.')
        else:
            messages.success(request, f'Invoice {ipr.invoice_number} saved as draft.')

        return redirect('incoming_payment_request_detail', pk=ipr.pk)


class IncomingPaymentRequestDetailView(LoginRequiredMixin, View):
    template_name = 'finance/incoming_payment_request_detail.html'

    def dispatch(self, request, *args, **kwargs):
        if not _can_manage_payment_requests(request.user):
            messages.error(request, 'Permission denied.')
            return redirect('finance_dashboard')
        return super().dispatch(request, *args, **kwargs)

    def get(self, request, pk):
        ipr = get_object_or_404(IncomingPaymentRequest, pk=pk)
        return render(request, self.template_name, {
            'ipr':       ipr,
            'documents': ipr.documents.all(),
            'is_finance': _is_finance(request.user),
            'is_ceo':    _is_ceo(request.user),
            'doc_type_choices': IncomingPaymentDocument.DocType.choices,
        })


class IncomingPaymentRequestSendView(LoginRequiredMixin, View):
    """Send/resend the invoice email to the customer."""

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
        messages.success(request,
            f'Invoice {ipr.invoice_number} emailed to {ipr.customer_email or ipr.customer_name}.')
        return redirect('incoming_payment_request_detail', pk=pk)


class IncomingPaymentRequestReminderView(LoginRequiredMixin, View):
    """Send a payment reminder (escalates in tone each time)."""

    def post(self, request, pk):
        if not _can_manage_payment_requests(request.user):
            messages.error(request, 'Permission denied.')
            return redirect('finance_dashboard')

        ipr = get_object_or_404(IncomingPaymentRequest, pk=pk)
        if ipr.status in (IncomingPaymentRequest.Status.PAID,
                          IncomingPaymentRequest.Status.CANCELLED):
            messages.warning(request, 'Cannot send reminder for a paid or cancelled invoice.')
            return redirect('incoming_payment_request_detail', pk=pk)

        _send_invoice_reminder_email(ipr)
        ipr.reminder_count     += 1
        ipr.last_reminder_sent  = timezone.now()
        if ipr.status == IncomingPaymentRequest.Status.DRAFT:
            ipr.status = IncomingPaymentRequest.Status.SENT
        ipr.save(update_fields=['reminder_count', 'last_reminder_sent', 'status', 'updated_at'])

        ordinals = {1: '1st', 2: '2nd', 3: '3rd'}
        label = ordinals.get(ipr.reminder_count, f'{ipr.reminder_count}th')
        messages.success(request,
            f'{label} reminder emailed to {ipr.customer_email or ipr.customer_name}.')
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

        messages.success(request,
            f'Invoice {ipr.invoice_number} marked as paid. '
            f'Amount: {ipr.currency} {ipr.total_amount:,.2f}.')
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
                dtype = request.POST.get(f'doc_type_{idx}',
                                         IncomingPaymentDocument.DocType.OTHER)
                dname = request.POST.get(f'doc_name_{idx}', f.name).strip() or f.name
                doc   = IncomingPaymentDocument(
                    payment_request=ipr, doc_type=dtype,
                    name=dname, uploaded_by=request.user,
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