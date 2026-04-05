from django.contrib.auth.mixins import LoginRequiredMixin
from django.views.generic import TemplateView, DetailView, View
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.db.models import Q, Sum
from django.utils import timezone
from apps.core.models import User
from apps.staff.models import StaffProfile, LeaveRequest, LeaveType, LeaveBalance


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_hr_user(user):
    return user.is_superuser or user.groups.filter(name='HR').exists()


def _get_viewable_leave_qs(user):
    """
    HR and superuser can view all leave requests.
    Direct supervisors can view leave requests for staff assigned to them.
    """
    if user.is_superuser or _is_hr_user(user):
        return LeaveRequest.objects.all()

    return LeaveRequest.objects.filter(
        staff__staffprofile__supervisor=user
    )


def _get_actionable_leave_qs(user):
    """
    Who the user can actually approve/reject.
    - superuser: all
    - HR: only direct supervisees in their unit
    - direct supervisors: their direct supervisees
    """
    if user.is_superuser:
        return LeaveRequest.objects.all()

    base_qs = LeaveRequest.objects.filter(
        staff__staffprofile__supervisor=user
    )

    if _is_hr_user(user):
        try:
            profile = user.staffprofile
        except StaffProfile.DoesNotExist:
            return LeaveRequest.objects.none()

        if profile.unit_id:
            return base_qs.filter(staff__staffprofile__unit_id=profile.unit_id)
        return base_qs

    return base_qs


def _user_can_approve(user):
    if user.is_superuser or _is_hr_user(user):
        return True

    return StaffProfile.objects.filter(
        supervisor=user,
        user__is_active=True
    ).exists()

# ---------------------------------------------------------------------------
# Staff Directory
# ---------------------------------------------------------------------------

class StaffDirectoryView(LoginRequiredMixin, TemplateView):
    template_name = 'staff/directory.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        q = self.request.GET.get('q', '')
        dept = self.request.GET.get('dept', '')
        unit = self.request.GET.get('unit', '')
        contract = self.request.GET.get('contract', '')

        staff = User.objects.filter(is_active=True, status='active').select_related(
            'staffprofile',
            'staffprofile__unit',
            'staffprofile__department',
            'staffprofile__position',
        )

        if q:
            staff = staff.filter(
                Q(first_name__icontains=q) | Q(last_name__icontains=q) |
                Q(email__icontains=q) | Q(employee_id__icontains=q)
            )
        if dept:
            staff = staff.filter(staffprofile__department_id=dept)
        if unit:
            staff = staff.filter(staffprofile__unit_id=unit)
        if contract:
            staff = staff.filter(staffprofile__contract_type=contract)

        from apps.organization.models import Department, Unit
        ctx['staff'] = staff.order_by('last_name')
        ctx['departments'] = Department.objects.filter(is_active=True)
        ctx['units'] = Unit.objects.filter(is_active=True)
        ctx['contract_types'] = StaffProfile.ContractType.choices
        ctx['q'] = q
        ctx['selected_dept'] = dept
        ctx['selected_unit'] = unit
        ctx['selected_contract'] = contract

        if _user_can_approve(self.request.user):
            ctx['pending_approvals_count'] = _get_actionable_leave_qs(self.request.user).filter(
                status='pending'
            ).count()

        return ctx


# ---------------------------------------------------------------------------
# Staff Detail
# ---------------------------------------------------------------------------

class StaffDetailView(LoginRequiredMixin, DetailView):
    model = User
    template_name = 'staff/staff_detail.html'
    context_object_name = 'staff_user'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        user = self.get_object()
        profile = getattr(user, 'staffprofile', None)
        ctx['profile'] = profile
        ctx['recent_tasks'] = user.assigned_tasks.order_by('-created_at')[:5]
        ctx['active_projects'] = user.projects.filter(status='active')[:5]
        ctx['leave_requests'] = user.leave_requests.order_by('-created_at')[:10]

        current_year = timezone.now().year
        raw_skills = getattr(user, 'skills', '') or ''
        ctx['staff_skills'] = [s.strip() for s in raw_skills.split(',') if s.strip()]

        ctx['leave_balances'] = LeaveBalance.objects.filter(
            staff=user, year=current_year
        ).select_related('leave_type')

        viewer = self.request.user
        ctx['viewer_can_approve'] = False

        if viewer != user:
            if viewer.is_superuser:
                ctx['viewer_can_approve'] = True
            else:
                try:
                    vp = viewer.staffprofile
                    if vp.can_approve_leave_for(user):
                        ctx['viewer_can_approve'] = True
                except StaffProfile.DoesNotExist:
                    pass

        if ctx['viewer_can_approve']:
            ctx['pending_leaves'] = user.leave_requests.filter(
                status='pending'
            ).select_related('leave_type').order_by('-created_at')
        else:
            ctx['pending_leaves'] = []

        return ctx


# ---------------------------------------------------------------------------
# Leave Request (staff submits own leave)
# ---------------------------------------------------------------------------

class LeaveRequestView(LoginRequiredMixin, View):
    template_name = 'staff/leave_request.html'

    def _build_context(self, request):
        current_year = timezone.now().year
        leave_types = LeaveType.objects.filter(is_active=True)

        for lt in leave_types:
            LeaveBalance.get_or_create_for(request.user, lt, current_year)

        balances = {
            b.leave_type_id: b
            for b in LeaveBalance.objects.filter(
                staff=request.user, year=current_year
            ).select_related('leave_type')
        }

        leave_types_with_balance = []
        for lt in leave_types:
            bal = balances.get(lt.id)
            leave_types_with_balance.append({
                'lt': lt,
                'balance': bal,
            })

        return {
            'leave_types': leave_types,
            'leave_types_with_balance': leave_types_with_balance,
            'my_requests': request.user.leave_requests.select_related(
                'leave_type', 'approved_by'
            ).order_by('-created_at')[:20],
            'current_year': current_year,
        }

    def get(self, request):
        return render(request, self.template_name, self._build_context(request))

    def post(self, request):
        cancel_id = request.POST.get('cancel_id')
        if cancel_id:
            lr = get_object_or_404(
                LeaveRequest, id=cancel_id, staff=request.user, status='pending'
            )
            lr.status = LeaveRequest.Status.CANCELLED
            lr.save()
            messages.success(request, 'Leave request cancelled.')
            return redirect('leave_requests')

        leave_type_id = request.POST.get('leave_type')
        if not leave_type_id:
            messages.error(request, 'Please select a leave type.')
            return render(request, self.template_name, self._build_context(request))

        leave_type = get_object_or_404(LeaveType, id=leave_type_id)
        start = request.POST.get('start_date', '')
        end = request.POST.get('end_date', '')

        try:
            from datetime import date
            start_d = date.fromisoformat(start)
            end_d = date.fromisoformat(end)
        except ValueError:
            messages.error(request, 'Invalid dates provided.')
            return render(request, self.template_name, self._build_context(request))

        if end_d < start_d:
            messages.error(request, 'End date cannot be before start date.')
            return render(request, self.template_name, self._build_context(request))

        days = (end_d - start_d).days + 1

        balance = LeaveBalance.get_or_create_for(request.user, leave_type, start_d.year)
        if leave_type.days_per_year > 0 and days > balance.days_remaining:
            messages.warning(
                request,
                f'You only have {balance.days_remaining} day(s) remaining for {leave_type.name}. '
                f'Your request for {days} day(s) has been submitted but may be rejected.'
            )

        LeaveRequest.objects.create(
            staff=request.user,
            leave_type=leave_type,
            start_date=start_d,
            end_date=end_d,
            days_requested=days,
            reason=request.POST.get('reason', ''),
            supporting_document=request.FILES.get('supporting_document'),
        )
        messages.success(request, 'Leave request submitted for approval.')
        return redirect('leave_requests')


# ---------------------------------------------------------------------------
# Leave Approval (supervisors / superusers)
# ---------------------------------------------------------------------------

class LeaveApprovalView(LoginRequiredMixin, View):
    template_name = 'staff/leave_approval.html'

    def _check_authority(self, request):
        if not _user_can_approve(request.user):
            messages.error(request, 'You do not have permission to view leave approvals.')
            return False
        return True

    def get(self, request):
        if not self._check_authority(request):
            return redirect('dashboard')

        staff_filter = request.GET.get('staff')

        # HR can view all; actionability is separate
        visible_qs = _get_viewable_leave_qs(request.user).select_related(
            'staff', 'leave_type', 'approved_by',
            'staff__staffprofile',
            'staff__staffprofile__position',
            'staff__staffprofile__unit',
            'staff__staffprofile__department',
        )

        pending = visible_qs.filter(status='pending')
        recent = visible_qs.filter(status__in=['approved', 'rejected'])

        if staff_filter:
            pending = pending.filter(staff_id=staff_filter)
            recent = recent.filter(staff_id=staff_filter)

        pending = pending.order_by('-created_at')
        recent = recent.order_by('-approval_date')[:20]

        actionable_ids = set(
            _get_actionable_leave_qs(request.user).values_list('id', flat=True)
        )

        ctx = {
            'pending_requests': pending,
            'recent_decisions': recent,
            'is_superuser_approver': request.user.is_superuser,
            'staff_filter': staff_filter,
            'actionable_ids': actionable_ids,
            'is_hr_user': _is_hr_user(request.user),
        }

        if staff_filter:
            try:
                ctx['filtered_staff'] = User.objects.get(pk=staff_filter)
            except (User.DoesNotExist, Exception):
                pass

        return render(request, self.template_name, ctx)

    def post(self, request):
        if not self._check_authority(request):
            return redirect('dashboard')

        lr_id = request.POST.get('leave_id')
        action = request.POST.get('action')

        if not lr_id or action not in ('approve', 'reject'):
            messages.error(request, 'Invalid request.')
            return redirect('leave_approval')

        lr = get_object_or_404(LeaveRequest, id=lr_id, status='pending')

        if not _get_actionable_leave_qs(request.user).filter(id=lr.id).exists():
            messages.error(
                request,
                'You can view this leave request, but you do not have authority to approve or reject it.'
            )
            return redirect('leave_approval')

        lr.approved_by = request.user
        lr.approval_date = timezone.now()
        lr.approval_notes = request.POST.get('notes', '')

        if action == 'approve':
            lr.status = LeaveRequest.Status.APPROVED
            lr.save()
            balance = LeaveBalance.get_or_create_for(lr.staff, lr.leave_type, lr.start_date.year)
            balance.recalculate_used()
            messages.success(request, f'Leave approved for {lr.staff.full_name}.')
        else:
            lr.status = LeaveRequest.Status.REJECTED
            lr.save()
            messages.warning(request, f'Leave rejected for {lr.staff.full_name}.')

        next_url = request.POST.get('next', '')
        if next_url and next_url.startswith('/'):
            return redirect(next_url)
        return redirect('leave_approval')