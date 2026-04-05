from decimal import Decimal, InvalidOperation

from django.contrib.auth.mixins import LoginRequiredMixin
from django.views.generic import TemplateView, View, DetailView
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.utils import timezone
from django.db.models import Sum, Q
from django.http import HttpResponseForbidden

from apps.finance.models import Budget, PurchaseRequest, Payment


def _is_finance(user):
    return user.is_superuser or user.groups.filter(name__in=['Finance', 'Admin']).exists()


def _can_view_purchase(user, purchase):
    if _is_finance(user):
        return True
    return purchase.requested_by_id == user.id


def _can_edit_purchase(user, purchase):
    """
    Requester can edit own draft or rejected request.
    Finance can edit any request.
    """
    if _is_finance(user):
        return True

    if purchase.requested_by_id != user.id:
        return False

    return purchase.status in [
        PurchaseRequest.Status.DRAFT,
        PurchaseRequest.Status.REJECTED,
    ]


def _validate_decimal(value, field_name='amount'):
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError(f'Please enter a valid {field_name}.')


# ── Dashboard ────────────────────────────────────────────────────────────────

class FinanceDashboardView(LoginRequiredMixin, TemplateView):
    template_name = 'finance/dashboard.html'

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
            'approved_count': PurchaseRequest.objects.filter(status='approved').count(),
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
            'recent_payments': Payment.objects.select_related(
                'paid_by', 'purchase_request', 'budget'
            ).order_by('-payment_date', '-created_at')[:8],
            'my_recent_requests': my_requests.select_related(
                'department', 'budget', 'project'
            ).order_by('-created_at')[:6],
            'my_pending_requests': my_requests.filter(status='submitted').count(),
            'my_draft_requests': my_requests.filter(status='draft').count(),
            'is_finance': _is_finance(self.request.user),
        })
        return ctx


# ── Budget List ──────────────────────────────────────────────────────────────

class BudgetListView(LoginRequiredMixin, TemplateView):
    template_name = 'finance/budget_list.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)

        year = int(self.request.GET.get('year', timezone.now().year))
        status = self.request.GET.get('status', '').strip()

        qs = Budget.objects.filter(fiscal_year=year).select_related(
            'department', 'unit', 'approved_by'
        )

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
        })
        return ctx


# ── Purchase Request List/Create ─────────────────────────────────────────────

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

        if _is_finance(request.user):
            visible_requests = PurchaseRequest.objects.select_related(
                'requested_by', 'department', 'budget', 'project', 'approved_by'
            ).order_by('-created_at')[:50]
        else:
            visible_requests = PurchaseRequest.objects.filter(
                requested_by=request.user
            ).select_related(
                'department', 'budget', 'project', 'approved_by'
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
        # cancel own draft
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

        if not title:
            messages.error(request, 'Title is required.')
            return render(request, self.template_name, self._ctx(request))

        if not description:
            messages.error(request, 'Description is required.')
            return render(request, self.template_name, self._ctx(request))

        if not justification:
            messages.error(request, 'Justification is required.')
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

        if status_value == PurchaseRequest.Status.DRAFT:
            messages.success(request, f'Draft "{pr.title}" saved.')
        else:
            messages.success(request, f'Purchase request "{pr.title}" submitted for approval.')

        return redirect('purchase_request')


# ── Purchase Request Detail ──────────────────────────────────────────────────

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
        ctx['can_edit_purchase'] = _can_edit_purchase(self.request.user, purchase)
        ctx['payments'] = purchase.payments.select_related('paid_by', 'budget').order_by('-payment_date', '-created_at')
        return ctx


# ── Purchase Approval / Status Update ────────────────────────────────────────

class PurchaseApprovalView(LoginRequiredMixin, View):
    def post(self, request, pk):
        if not _is_finance(request.user):
            messages.error(request, 'Permission denied.')
            return redirect('finance_dashboard')

        pr = get_object_or_404(PurchaseRequest, pk=pk)
        action = request.POST.get('action')
        next_url = request.POST.get('next', '')

        if action == 'approve':
            pr.status = PurchaseRequest.Status.APPROVED
            pr.approved_by = request.user
            pr.approval_date = timezone.now()
            pr.approval_notes = request.POST.get('notes', '')
            pr.save()
            messages.success(request, f'"{pr.title}" approved.')

        elif action == 'reject':
            pr.status = PurchaseRequest.Status.REJECTED
            pr.approved_by = request.user
            pr.approval_date = timezone.now()
            pr.approval_notes = request.POST.get('notes', '')
            pr.save()
            messages.warning(request, f'"{pr.title}" rejected.')

        elif action == 'update_status':
            new_status = request.POST.get('status', '')
            if new_status in dict(PurchaseRequest.Status.choices):
                pr.status = new_status
                pr.notes = request.POST.get('notes', pr.notes)
                pr.save(update_fields=['status', 'notes', 'updated_at'])
                messages.success(request, 'Purchase status updated.')

        return redirect(next_url if next_url.startswith('/') else 'finance_dashboard')


# ── Payment List ─────────────────────────────────────────────────────────────

class PaymentListView(LoginRequiredMixin, TemplateView):
    template_name = 'finance/payment_list.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)

        q = self.request.GET.get('q', '').strip()
        method = self.request.GET.get('method', '').strip()
        year = self.request.GET.get('year', '').strip()

        qs = Payment.objects.select_related(
            'paid_by', 'budget', 'purchase_request'
        )

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
        })
        return ctx