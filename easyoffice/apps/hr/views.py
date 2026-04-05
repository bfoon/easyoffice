from calendar import monthrange
from django.contrib.auth.mixins import LoginRequiredMixin
from django.views.generic import TemplateView, DetailView, View
from django.shortcuts import redirect, get_object_or_404
from django.contrib import messages
from django.utils import timezone
from django.db.models import Q, Sum
from django.http import HttpResponseForbidden
from apps.hr.models import PerformanceAppraisal, PayrollRecord, AttendanceRecord
from apps.core.models import User
from apps.staff.models import LeaveRequest


def is_hr_user(user):
    return user.is_superuser or user.groups.filter(name='HR').exists()


def is_supervisor_user(user):
    if not user.is_authenticated:
        return False
    if hasattr(user, 'staff_profile'):
        return getattr(user.staff_profile, 'is_supervisor_role', False)
    if hasattr(user, 'staffprofile'):
        return getattr(user.staffprofile, 'is_supervisor_role', False)
    return False


def get_supervisee_queryset(user):
    """
    Uses appraisal relationships as the current supervisor mapping source.
    Replace this later with a direct reporting-line field if available.
    """
    supervisee_ids = PerformanceAppraisal.objects.filter(
        supervisor=user
    ).values_list('staff_id', flat=True)
    return User.objects.filter(pk__in=supervisee_ids, is_active=True).distinct()


def can_view_appraisal(user, appraisal):
    if is_hr_user(user):
        return True
    if appraisal.staff == user:
        return True
    if appraisal.supervisor == user:
        return True
    return False


def can_view_attendance_sheet(request_user, target_user):
    if is_hr_user(request_user):
        return True
    if request_user == target_user:
        return True
    if is_supervisor_user(request_user):
        return get_supervisee_queryset(request_user).filter(pk=target_user.pk).exists()
    return False


class HRDashboardView(LoginRequiredMixin, TemplateView):
    template_name = 'hr/hr_dashboard.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        user = self.request.user
        now = timezone.now()
        year = now.year
        month = now.month
        today = now.date()

        ctx['is_hr_user'] = is_hr_user(user)
        ctx['is_supervisor_user'] = is_supervisor_user(user)
        ctx['is_staff_user'] = not is_hr_user(user) and not is_supervisor_user(user)

        if is_hr_user(user):
            # Full HR visibility
            ctx['appraisals_this_year'] = PerformanceAppraisal.objects.filter(year=year).count()
            ctx['pending_appraisals'] = PerformanceAppraisal.objects.filter(
                year=year,
                status__in=['draft', 'self_review', 'supervisor_review', 'hr_review']
            ).count()
            ctx['payroll_this_month'] = PayrollRecord.objects.filter(
                period_year=year, period_month=month
            ).count()
            ctx['attendance_today_count'] = AttendanceRecord.objects.filter(date=today).count()
            ctx['present_today_count'] = AttendanceRecord.objects.filter(
                date=today, status__in=['present', 'late', 'remote']
            ).count()
            ctx['late_today_count'] = AttendanceRecord.objects.filter(date=today, status='late').count()
            ctx['absent_today_count'] = AttendanceRecord.objects.filter(date=today, status='absent').count()

            ctx['recent_appraisals'] = PerformanceAppraisal.objects.select_related(
                'staff', 'supervisor'
            ).order_by('-created_at')[:8]

            ctx['recent_attendance'] = AttendanceRecord.objects.select_related(
                'staff', 'marked_by'
            ).order_by('-date', 'staff__last_name')[:10]

            ctx['my_appraisals'] = PerformanceAppraisal.objects.filter(
                staff=user
            ).select_related('supervisor').order_by('-year')[:5]

        elif is_supervisor_user(user):
            supervisees = get_supervisee_queryset(user)

            ctx['supervisee_count'] = supervisees.count()
            ctx['appraisals_this_year'] = PerformanceAppraisal.objects.filter(
                year=year,
                supervisor=user
            ).count()
            ctx['pending_appraisals'] = PerformanceAppraisal.objects.filter(
                year=year,
                supervisor=user,
                status__in=['supervisor_review', 'hr_review']
            ).count()
            ctx['attendance_today_count'] = AttendanceRecord.objects.filter(
                date=today,
                staff__in=supervisees
            ).count()

            ctx['recent_appraisals'] = PerformanceAppraisal.objects.filter(
                Q(supervisor=user) | Q(staff=user)
            ).select_related('staff', 'supervisor').order_by('-created_at')[:8]

            ctx['recent_attendance'] = AttendanceRecord.objects.filter(
                staff__in=supervisees
            ).select_related('staff', 'marked_by').order_by('-date', 'staff__last_name')[:10]

            ctx['my_appraisals'] = PerformanceAppraisal.objects.filter(
                staff=user
            ).select_related('supervisor').order_by('-year')[:5]

        else:
            # Staff self-service only
            ctx['my_appraisals'] = PerformanceAppraisal.objects.filter(
                staff=user
            ).select_related('supervisor').order_by('-year')[:8]

            ctx['my_attendance_today'] = AttendanceRecord.objects.filter(
                staff=user,
                date=today
            ).first()

            ctx['my_attendance_this_month'] = AttendanceRecord.objects.filter(
                staff=user,
                date__year=year,
                date__month=month
            ).order_by('-date')[:10]

            ctx['my_payroll_this_month'] = PayrollRecord.objects.filter(
                staff=user,
                period_year=year,
                period_month=month
            ).first()

        return ctx


class AppraisalListView(LoginRequiredMixin, TemplateView):
    template_name = 'hr/appraisal_list.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        user = self.request.user
        year = int(self.request.GET.get('year', timezone.now().year))

        if is_hr_user(user):
            appraisals = PerformanceAppraisal.objects.filter(year=year)
        elif is_supervisor_user(user):
            appraisals = PerformanceAppraisal.objects.filter(
                year=year
            ).filter(
                Q(supervisor=user) | Q(staff=user)
            )
        else:
            appraisals = PerformanceAppraisal.objects.filter(year=year, staff=user)

        ctx['appraisals'] = appraisals.select_related('staff', 'supervisor').order_by('status', 'staff__last_name')
        ctx['year'] = year
        ctx['year_range'] = range(timezone.now().year, timezone.now().year - 5, -1)
        ctx['is_hr_user'] = is_hr_user(user)
        ctx['is_supervisor_user'] = is_supervisor_user(user)
        return ctx


class AppraisalDetailView(LoginRequiredMixin, DetailView):
    model = PerformanceAppraisal
    template_name = 'hr/appraisal_detail.html'
    context_object_name = 'appraisal'

    def dispatch(self, request, *args, **kwargs):
        appraisal = self.get_object()
        if not can_view_appraisal(request.user, appraisal):
            return HttpResponseForbidden('You do not have permission to view this appraisal.')
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        appraisal = self.object
        user = self.request.user

        ctx['kpis'] = appraisal.kpis.all()
        ctx['can_self_review'] = (user == appraisal.staff and appraisal.status == 'self_review')
        ctx['can_supervisor_review'] = (user == appraisal.supervisor and appraisal.status == 'supervisor_review')
        ctx['can_hr_review'] = (is_hr_user(user) and appraisal.status == 'hr_review')
        ctx['is_hr_user'] = is_hr_user(user)
        ctx['is_supervisor_user'] = is_supervisor_user(user)
        return ctx

    def post(self, request, pk):
        appraisal = get_object_or_404(PerformanceAppraisal, pk=pk)

        if not can_view_appraisal(request.user, appraisal):
            return HttpResponseForbidden('You do not have permission to act on this appraisal.')

        action = request.POST.get('action')

        if action == 'self_submit' and request.user == appraisal.staff:
            appraisal.self_achievements = request.POST.get('self_achievements', '')
            appraisal.self_challenges = request.POST.get('self_challenges', '')
            appraisal.self_goals = request.POST.get('self_goals', '')
            appraisal.self_rating = request.POST.get('self_rating', '')
            appraisal.status = 'supervisor_review'
            appraisal.self_submitted_at = timezone.now()
            appraisal.save()
            messages.success(request, 'Self-appraisal submitted.')

        elif action == 'supervisor_submit' and request.user == appraisal.supervisor:
            appraisal.supervisor_comments = request.POST.get('supervisor_comments', '')
            appraisal.supervisor_rating = request.POST.get('supervisor_rating', '')
            appraisal.areas_of_improvement = request.POST.get('areas_of_improvement', '')
            appraisal.training_recommended = request.POST.get('training_recommended', '')
            appraisal.overall_rating = request.POST.get('overall_rating', '')
            appraisal.status = 'hr_review'
            appraisal.supervisor_submitted_at = timezone.now()
            appraisal.save()
            messages.success(request, 'Supervisor review submitted.')

        elif action == 'hr_finalize' and is_hr_user(request.user):
            appraisal.hr_comments = request.POST.get('hr_comments', '')
            appraisal.hr_reviewed_by = request.user
            appraisal.hr_reviewed_at = timezone.now()
            appraisal.completed_at = timezone.now()
            appraisal.status = 'completed'
            if not appraisal.overall_rating:
                appraisal.overall_rating = request.POST.get('overall_rating', appraisal.supervisor_rating or '')
            appraisal.save()
            messages.success(request, 'HR review completed.')

        return redirect('appraisal_detail', pk=pk)


class PayrollView(LoginRequiredMixin, TemplateView):
    template_name = 'hr/payroll.html'

    def dispatch(self, request, *args, **kwargs):
        # Only HR/superuser can access payroll module
        if not is_hr_user(request.user):
            return HttpResponseForbidden('You do not have permission to view payroll.')
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        year = int(self.request.GET.get('year', timezone.now().year))
        month = int(self.request.GET.get('month', timezone.now().month))

        records = PayrollRecord.objects.filter(
            period_year=year,
            period_month=month
        ).select_related('staff').order_by('-period_year', '-period_month', 'staff__last_name')

        ctx['records'] = records
        ctx['year'] = year
        ctx['month'] = month
        ctx['total_gross'] = records.aggregate(total=Sum('gross_salary'))['total'] or 0
        ctx['total_net'] = records.aggregate(total=Sum('net_salary'))['total'] or 0
        return ctx


class AttendanceView(LoginRequiredMixin, TemplateView):
    template_name = 'hr/attendance.html'

    def dispatch(self, request, *args, **kwargs):
        if not is_hr_user(request.user):
            messages.error(request, 'Only HR can manage staff attendance.')
            return redirect('hr_dashboard')
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        date_str = self.request.GET.get('date')
        q = self.request.GET.get('q', '').strip()

        try:
            selected_date = timezone.datetime.strptime(date_str, '%Y-%m-%d').date() if date_str else timezone.now().date()
        except ValueError:
            selected_date = timezone.now().date()

        staff_qs = User.objects.filter(is_active=True, status='active').order_by('first_name', 'last_name')
        if q:
            staff_qs = staff_qs.filter(
                Q(first_name__icontains=q) |
                Q(last_name__icontains=q) |
                Q(email__icontains=q) |
                Q(username__icontains=q)
            )

        summary = AttendanceRecord.objects.filter(date=selected_date)

        attendance_map = {
            str(rec.staff_id): rec
            for rec in summary.select_related('staff')
        }

        # Inject approved leave into attendance display where no explicit attendance exists yet
        approved_leaves = LeaveRequest.objects.filter(
            status='approved',
            start_date__lte=selected_date,
            end_date__gte=selected_date,
            staff__is_active=True,
        ).select_related('staff', 'leave_type')

        leave_map = {str(lr.staff_id): lr for lr in approved_leaves}

        staff_rows = []
        for staff in staff_qs:
            record = attendance_map.get(str(staff.id))
            leave_request = leave_map.get(str(staff.id))

            derived_leave_record = None
            if not record and leave_request:
                derived_leave_record = {
                    'status': 'leave',
                    'status_label': f'On {leave_request.leave_type.name}',
                    'check_in': None,
                    'check_out': None,
                    'minutes_late': 0,
                    'worked_hours': 0,
                    'notes': leave_request.reason,
                }

            staff_rows.append({
                'staff': staff,
                'record': record,
                'leave_request': leave_request,
                'derived_leave_record': derived_leave_record,
            })

        ctx['selected_date'] = selected_date
        ctx['staff_rows'] = staff_rows
        ctx['attendance_statuses'] = AttendanceRecord.Status.choices
        ctx['q'] = q
        ctx['summary'] = summary
        ctx['summary_count'] = summary.count()
        ctx['present_count'] = summary.filter(status__in=['present', 'late', 'remote']).count()
        ctx['late_count'] = summary.filter(status='late').count()

        # Count approved leave even if no attendance record exists
        ctx['leave_count'] = approved_leaves.count()
        ctx['absent_count'] = summary.filter(status='absent').count()
        return ctx

    def post(self, request, *args, **kwargs):
        if not is_hr_user(request.user):
            messages.error(request, 'Only HR can manage staff attendance.')
            return redirect('hr_dashboard')

        date_str = request.POST.get('date')
        try:
            selected_date = timezone.datetime.strptime(date_str, '%Y-%m-%d').date()
        except (ValueError, TypeError):
            selected_date = timezone.now().date()

        approved_leave_staff_ids = set(
            LeaveRequest.objects.filter(
                status='approved',
                start_date__lte=selected_date,
                end_date__gte=selected_date,
            ).values_list('staff_id', flat=True)
        )

        for key, value in request.POST.items():
            if not key.startswith('status_'):
                continue

            staff_id = key.replace('status_', '')
            status = value
            check_in = request.POST.get(f'check_in_{staff_id}') or None
            check_out = request.POST.get(f'check_out_{staff_id}') or None
            notes = request.POST.get(f'notes_{staff_id}', '').strip()

            staff = User.objects.filter(pk=staff_id, is_active=True).first()
            if not staff:
                continue

            # Default approved leave to "leave" unless HR deliberately changes it
            if staff.id in approved_leave_staff_ids and status == 'present':
                status = 'leave'

            record, _ = AttendanceRecord.objects.get_or_create(
                staff=staff,
                date=selected_date,
                defaults={
                    'status': status,
                    'marked_by': request.user,
                    'notes': notes,
                }
            )

            record.status = status
            record.notes = notes
            record.marked_by = request.user

            if check_in:
                try:
                    record.check_in = timezone.datetime.strptime(check_in, '%H:%M').time()
                except ValueError:
                    record.check_in = None
            else:
                record.check_in = None

            if check_out:
                try:
                    record.check_out = timezone.datetime.strptime(check_out, '%H:%M').time()
                except ValueError:
                    record.check_out = None
            else:
                record.check_out = None

            record.calculate_worked_hours()

            if status == 'late' and record.check_in:
                official_start = timezone.datetime.strptime('08:30', '%H:%M').time()
                actual_minutes = record.check_in.hour * 60 + record.check_in.minute
                official_minutes = official_start.hour * 60 + official_start.minute
                record.minutes_late = max(0, actual_minutes - official_minutes)
            else:
                record.minutes_late = 0

            record.save()

        messages.success(request, f'Attendance for {selected_date} saved successfully.')
        return redirect(f'{request.path}?date={selected_date.isoformat()}')

class AttendanceSheetView(LoginRequiredMixin, TemplateView):
    template_name = 'hr/attendance_sheet.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)

        request_user = self.request.user
        target_staff_id = self.request.GET.get('staff')
        today = timezone.now().date()

        try:
            year = int(self.request.GET.get('year', today.year))
        except (TypeError, ValueError):
            year = today.year

        try:
            month = int(self.request.GET.get('month', today.month))
        except (TypeError, ValueError):
            month = today.month

        if month < 1 or month > 12:
            month = today.month

        if target_staff_id:
            staff = get_object_or_404(User, pk=target_staff_id, is_active=True)
        else:
            staff = request_user

        if not can_view_attendance_sheet(request_user, staff):
            raise HttpResponseForbidden('You do not have permission to generate this attendance sheet.')

        start_date = timezone.datetime(year, month, 1).date()
        end_date = timezone.datetime(year, month, monthrange(year, month)[1]).date()

        records_qs = AttendanceRecord.objects.filter(
            staff=staff,
            date__range=[start_date, end_date]
        ).order_by('date')

        record_map = {rec.date: rec for rec in records_qs}

        days = []
        present_count = 0
        late_count = 0
        absent_count = 0
        leave_count = 0
        remote_count = 0
        total_hours = 0

        for day_num in range(1, monthrange(year, month)[1] + 1):
            current_date = timezone.datetime(year, month, day_num).date()
            rec = record_map.get(current_date)

            if rec:
                total_hours += float(rec.worked_hours or 0)
                if rec.status == 'present':
                    present_count += 1
                elif rec.status == 'late':
                    late_count += 1
                elif rec.status == 'absent':
                    absent_count += 1
                elif rec.status == 'leave':
                    leave_count += 1
                elif rec.status == 'remote':
                    remote_count += 1

            days.append({
                'date': current_date,
                'record': rec,
            })

        if is_hr_user(request_user):
            selectable_staff = User.objects.filter(
                is_active=True,
                status='active'
            ).order_by('first_name', 'last_name')
        elif is_supervisor_user(request_user):
            supervisees = get_supervisee_queryset(request_user)
            selectable_staff = User.objects.filter(
                Q(pk=request_user.pk) | Q(pk__in=supervisees.values_list('pk', flat=True)),
                is_active=True
            ).distinct().order_by('first_name', 'last_name')
        else:
            selectable_staff = User.objects.filter(pk=request_user.pk)

        ctx.update({
            'sheet_staff': staff,
            'selected_year': year,
            'selected_month': month,
            'start_date': start_date,
            'end_date': end_date,
            'days': days,
            'selectable_staff': selectable_staff,
            'present_count': present_count,
            'late_count': late_count,
            'absent_count': absent_count,
            'leave_count': leave_count,
            'remote_count': remote_count,
            'total_hours': round(total_hours, 2),
            'month_choices': [
                (1, 'January'), (2, 'February'), (3, 'March'), (4, 'April'),
                (5, 'May'), (6, 'June'), (7, 'July'), (8, 'August'),
                (9, 'September'), (10, 'October'), (11, 'November'), (12, 'December')
            ],
            'year_choices': range(today.year, today.year - 5, -1),
            'can_manage_attendance': is_hr_user(request_user),
            'is_hr_user': is_hr_user(request_user),
            'is_supervisor_user': is_supervisor_user(request_user),
        })
        return ctx