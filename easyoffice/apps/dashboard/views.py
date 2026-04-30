from django.contrib.auth.mixins import LoginRequiredMixin
from django.views.generic import TemplateView, View
from django.db.models import Count, Q
from django.utils import timezone
from django.shortcuts import redirect, get_object_or_404

from apps.core.models import User
from apps.tasks.models import Task
from apps.projects.models import Project
from apps.staff.models import LeaveRequest
from apps.opportunities.models import OpportunityMatch, OpportunitySource


class DashboardView(LoginRequiredMixin, TemplateView):
    def _is_admin_user(self, user):
        return user.is_superuser or user.groups.filter(
            name__in=['CEO', 'Admin', 'Office Manager']
        ).exists()

    def _get_staff_profile(self, user):
        return getattr(user, 'staffprofile', None)

    def dispatch(self, request, *args, **kwargs):
        # IMPORTANT:
        # admin users should go to the real admin dashboard
        # because that is where all_active_tasks and the full office view are built
        if self._is_admin_user(request.user):
            return redirect('admin_dashboard')
        return super().dispatch(request, *args, **kwargs)

    def get_template_names(self):
        user = self.request.user

        profile = self._get_staff_profile(user)
        if profile and profile.is_supervisor_role:
            return ['dashboard/supervisor_dashboard.html']

        return ['dashboard/staff_dashboard.html']

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        user = self.request.user
        now = timezone.now()
        today = now.date()

        ctx['now'] = now
        ctx['is_admin_user'] = self._is_admin_user(user)

        # ── Signature snapshot ───────────────────────────────────────────
        # Counts are lifetime (matches the wording on the tiles). If you
        # later want a "this month" cut, filter by created_at__gte=month_start
        # using the same `month_start` we already compute for invoices.
        try:
            from apps.files.models import SignatureRequest
            sig_qs = SignatureRequest.objects.all()
            ctx['sig_signed_flow'] = sig_qs.filter(status='completed').count()
            # Pending = anything currently in-flight: sent / partially signed / viewed.
            # We treat any status that isn't a terminal one as pending.
            ctx['sig_pending'] = sig_qs.exclude(
                status__in=['completed', 'cancelled', 'expired', 'declined']
            ).count()
            ctx['sig_voided'] = sig_qs.filter(status__in=['cancelled', 'expired']).count()
        except Exception:
            ctx['sig_signed_flow'] = 0
            ctx['sig_pending'] = 0
            ctx['sig_voided'] = 0

        # Quick-sign count — try a dedicated model first, fall back to
        # AuditLog entries (most installations log quick-sign actions).
        try:
            # Preferred: a real QuickSignSession model
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

        profile = self._get_staff_profile(user)
        ctx['staff_profile'] = profile
        ctx['is_supervisor_user'] = bool(profile and profile.is_supervisor_role)

        my_tasks = user.assigned_tasks.all()
        ctx['my_tasks_total'] = my_tasks.count()
        ctx['my_tasks_todo'] = my_tasks.filter(status='todo').count()
        ctx['my_tasks_in_progress'] = my_tasks.filter(status='in_progress').count()
        ctx['my_tasks_review'] = my_tasks.filter(status='review').count()
        ctx['my_tasks_done'] = my_tasks.filter(status='done').count()
        ctx['my_tasks_overdue'] = my_tasks.filter(
            status__in=['todo', 'in_progress', 'review'],
            due_date__lt=now
        ).count()
        ctx['my_tasks_due_today'] = my_tasks.filter(
            status__in=['todo', 'in_progress', 'review'],
            due_date__date=today
        ).count()

        ctx['recent_tasks'] = my_tasks.filter(
            status__in=['todo', 'in_progress', 'review']
        ).select_related(
            'assigned_by', 'project', 'category'
        ).order_by(
            'due_date', '-created_at'
        )[:6]

        my_projects = user.projects.filter(status='active').select_related(
            'project_manager'
        ).order_by('-updated_at')

        ctx['my_projects'] = my_projects[:4]
        ctx['my_projects_count'] = my_projects.count()

        ctx['recent_notifications'] = user.core_notifications.filter(
            is_read=False
        ).order_by('-created_at')[:5]

        ctx['my_pending_leave_requests'] = user.leave_requests.filter(status='pending').count()
        ctx['my_approved_leave_requests'] = user.leave_requests.filter(status='approved').count()
        ctx['my_recent_leave_requests'] = user.leave_requests.select_related(
            'leave_type', 'approved_by'
        ).order_by('-created_at')[:5]

        if profile and profile.is_supervisor_role:
            supervisee_profiles = user.supervisees.select_related(
                'user', 'unit', 'department', 'position'
            ).filter(
                user__is_active=True,
                user__status='active'
            )

            supervisee_user_ids = [sp.user_id for sp in supervisee_profiles]

            ctx['team_count'] = supervisee_profiles.count()

            team_tasks = Task.objects.filter(
                assigned_to_id__in=supervisee_user_ids
            )

            ctx['team_tasks_total'] = team_tasks.count()
            ctx['team_tasks_overdue'] = team_tasks.filter(
                status__in=['todo', 'in_progress', 'review'],
                due_date__lt=now
            ).count()
            ctx['team_tasks_due_today'] = team_tasks.filter(
                status__in=['todo', 'in_progress', 'review'],
                due_date__date=today
            ).count()

            ctx['team_recent_tasks'] = team_tasks.select_related(
                'assigned_to', 'assigned_by', 'project'
            ).order_by(
                'due_date', '-updated_at'
            )[:6]

            ctx['team_recent_leave_requests'] = LeaveRequest.objects.filter(
                staff_id__in=supervisee_user_ids
            ).select_related(
                'staff', 'leave_type', 'approved_by'
            ).order_by('-created_at')[:6]

            ctx['team_pending_leave_requests'] = LeaveRequest.objects.filter(
                staff_id__in=supervisee_user_ids,
                status='pending'
            ).count()

            ctx['team_members'] = [sp.user for sp in supervisee_profiles[:8]]

            ctx['opportunity_match_count'] = OpportunityMatch.objects.filter(is_read=False).count()
            ctx['recent_opportunity_matches'] = OpportunityMatch.objects.select_related(
                'source', 'keyword'
            ).order_by('-detected_at')[:6]
            ctx['active_monitor_sources'] = OpportunitySource.objects.filter(is_active=True).count()

        return ctx


class AdminDashboardView(LoginRequiredMixin, TemplateView):
    template_name = 'dashboard/admin_dashboard.html'

    def dispatch(self, request, *args, **kwargs):
        if not (
            request.user.is_superuser or
            request.user.groups.filter(name__in=['CEO', 'Admin', 'Office Manager']).exists()
        ):
            from django.http import HttpResponseForbidden
            return HttpResponseForbidden('You do not have permission to access this dashboard.')
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        now = timezone.now()
        today = now.date()
        req = self.request

        status_filter = (req.GET.get('task_status') or '').strip()
        priority_filter = (req.GET.get('task_priority') or '').strip()
        unit_filter = (req.GET.get('unit') or '').strip()
        dept_filter = (req.GET.get('dept') or '').strip()
        search_q = (req.GET.get('tq') or '').strip()

        open_tasks = Task.objects.select_related(
            'assigned_to',
            'assigned_by',
            'assigned_to__staffprofile',
            'assigned_to__staffprofile__unit',
            'assigned_to__staffprofile__department',
            'assigned_to__staffprofile__supervisor',
            'project',
            'category',
        ).exclude(
            status__in=['done', 'cancelled']
        )

        filtered_tasks = open_tasks

        if status_filter:
            filtered_tasks = filtered_tasks.filter(status=status_filter)

        if priority_filter:
            filtered_tasks = filtered_tasks.filter(priority=priority_filter)

        if unit_filter:
            filtered_tasks = filtered_tasks.filter(
                assigned_to__staffprofile__unit_id=unit_filter
            )

        if dept_filter:
            filtered_tasks = filtered_tasks.filter(
                assigned_to__staffprofile__department_id=dept_filter
            )

        if search_q:
            filtered_tasks = filtered_tasks.filter(
                Q(title__icontains=search_q) |
                Q(description__icontains=search_q) |
                Q(assigned_to__first_name__icontains=search_q) |
                Q(assigned_to__last_name__icontains=search_q) |
                Q(project__name__icontains=search_q)
            )

        filtered_tasks = filtered_tasks.order_by('due_date', '-created_at').distinct()

        task_list = list(filtered_tasks[:50])
        for task in task_list:
            task.progress_pct = task.progress_pct or 0

        total_staff_qs = User.objects.filter(is_active=True, status='active')
        total_projects_qs = Project.objects.all()

        ctx['now'] = now
        ctx['dashboard_path'] = req.path

        ctx['total_staff'] = total_staff_qs.count()
        ctx['total_active_projects'] = total_projects_qs.filter(status='active').count()
        ctx['total_tasks_open'] = open_tasks.count()
        ctx['total_tasks_overdue'] = open_tasks.filter(due_date__lt=now).count()
        ctx['total_tasks_due_today'] = open_tasks.filter(due_date__date=today).count()

        ctx['all_active_tasks'] = task_list
        ctx['all_tasks_count'] = filtered_tasks.count()

        ctx['opportunity_match_count'] = OpportunityMatch.objects.filter(is_read=False).count()
        ctx['recent_opportunity_matches'] = OpportunityMatch.objects.select_related(
            'source', 'keyword'
        ).order_by('-detected_at')[:10]
        ctx['active_monitor_sources'] = OpportunitySource.objects.filter(is_active=True).count()

        ctx['priority_counts'] = {
            'critical': open_tasks.filter(priority='critical').count(),
            'urgent': open_tasks.filter(priority='urgent').count(),
            'high': open_tasks.filter(priority='high').count(),
            'medium': open_tasks.filter(priority='medium').count(),
            'low': open_tasks.filter(priority='low').count(),
        }

        try:
            from apps.organization.models import Unit, Department
            units = Unit.objects.filter(is_active=True).order_by('name')
            depts = Department.objects.filter(is_active=True).order_by('name')
        except Exception:
            units, depts = [], []

        unit_task_data = []
        for unit in units:
            unit_tasks = open_tasks.filter(assigned_to__staffprofile__unit=unit)
            total = unit_tasks.count()
            if total == 0:
                continue

            overdue = unit_tasks.filter(due_date__lt=now).count()

            unit_head = None
            try:
                from apps.staff.models import StaffProfile
                head_profile = StaffProfile.objects.filter(
                    unit=unit,
                    is_supervisor_role=True,
                    user__is_active=True,
                ).select_related('user').first()
                unit_head = head_profile.user if head_profile else None
            except Exception:
                pass

            unit_task_data.append({
                'unit': unit,
                'total': total,
                'overdue': overdue,
                'head': unit_head,
                'critical': unit_tasks.filter(priority__in=['critical', 'urgent']).count(),
            })

        ctx['unit_task_data'] = sorted(unit_task_data, key=lambda x: (-x['overdue'], -x['total']))

        ctx['overdue_tasks'] = open_tasks.filter(
            due_date__lt=now
        ).select_related(
            'assigned_to',
            'assigned_to__staffprofile',
            'assigned_to__staffprofile__supervisor',
            'assigned_to__staffprofile__unit',
            'assigned_by',
            'project',
        ).order_by('due_date')[:30]

        ctx['projects_by_status'] = total_projects_qs.values('status').annotate(
            count=Count('id')
        ).order_by('status')

        ctx['latest_projects'] = total_projects_qs.select_related(
            'project_manager'
        ).order_by('-created_at')[:5]

        ctx['office_pending_leave_requests'] = LeaveRequest.objects.filter(
            status='pending'
        ).count()

        ctx['office_recent_leave_requests'] = LeaveRequest.objects.select_related(
            'staff', 'leave_type', 'approved_by'
        ).order_by('-created_at')[:8]

        ctx['filter_units'] = units
        ctx['filter_depts'] = depts
        ctx['status_choices'] = Task.Status.choices
        ctx['priority_choices'] = Task.Priority.choices
        ctx['status_filter'] = status_filter
        ctx['priority_filter'] = priority_filter
        ctx['unit_filter'] = unit_filter
        ctx['dept_filter'] = dept_filter
        ctx['search_q'] = search_q

        return ctx


class TaskFollowUpView(LoginRequiredMixin, View):
    def dispatch(self, request, *args, **kwargs):
        if not (
            request.user.is_superuser or
            request.user.groups.filter(name__in=['CEO','Admin','Office Manager']).exists()
        ):
            from django.http import HttpResponseForbidden
            return HttpResponseForbidden()
        return super().dispatch(request, *args, **kwargs)

    def post(self, request):
        from django.contrib import messages
        from django.shortcuts import redirect, get_object_or_404
        from django.core.mail import EmailMessage
        from django.conf import settings

        task_id   = request.POST.get('task_id', '').strip()
        rec_type  = request.POST.get('recipient_type', 'supervisor')
        note      = request.POST.get('note', '').strip()

        task = get_object_or_404(Task, pk=task_id)
        org_name  = getattr(settings,'ORGANISATION_NAME',
                            getattr(settings,'OFFICE_NAME','EasyOffice'))
        org_email = getattr(settings,'DEFAULT_FROM_EMAIL',
                            f'admin@{org_name.lower().replace(" ","")}.org')
        sender    = request.user.full_name

        recipient_user = None
        profile = getattr(task.assigned_to, 'staffprofile', None)

        if rec_type == 'supervisor' and profile:
            recipient_user = getattr(profile, 'supervisor', None)
        elif rec_type == 'unit_head' and profile and profile.unit:
            try:
                from apps.staff.models import StaffProfile
                head = StaffProfile.objects.filter(
                    unit=profile.unit, is_supervisor_role=True
                ).select_related('user').first()
                recipient_user = head.user if head else None
            except Exception:
                pass
        elif rec_type == 'assigned_to':
            recipient_user = task.assigned_to

        if not recipient_user or not getattr(recipient_user, 'email', ''):
            messages.error(request, 'Could not find a valid email address for that recipient.')
            return redirect(request.META.get('HTTP_REFERER', 'admin_dashboard'))

        overdue_days = 0
        if task.due_date and task.due_date < timezone.now():
            overdue_days = (timezone.now() - task.due_date).days

        due_str  = task.due_date.strftime('%d %B %Y at %H:%M') if task.due_date else 'No due date set'
        task_url = request.build_absolute_uri(f'/tasks/{task.pk}/')
        rec_name = getattr(recipient_user, 'full_name', str(recipient_user))

        overdue_banner = ''
        if overdue_days:
            overdue_banner = f"""
<div style="background:#fef2f2;border:1.5px solid #fecaca;border-radius:10px;padding:14px 18px;margin:16px 0;font-size:14px;color:#991b1b">
  ⚠️ <strong>This task is {overdue_days} day{'s' if overdue_days != 1 else ''} overdue.</strong>
  It was due on {due_str}. Immediate action is required.
</div>"""

        note_block = ''
        if note:
            note_block = f"""
<div style="background:#fffbeb;border:1px solid #fde68a;border-radius:10px;padding:14px 18px;margin:16px 0;font-size:14px;color:#78350f">
  <strong>Message from {sender}:</strong><br>{note}
</div>"""

        html = f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"/>
<style>
body{{margin:0;padding:0;background:#f1f5f9;font-family:'Segoe UI',Arial,sans-serif;}}
.w{{max-width:600px;margin:28px auto;background:#fff;border-radius:16px;overflow:hidden;box-shadow:0 4px 24px rgba(0,0,0,.08);}}
.hdr{{background:linear-gradient(135deg,#1e3a5f,#0284c7);padding:32px 36px;}}
.hdr h1{{margin:0;color:#fff;font-size:20px;font-weight:800;}}
.hdr p{{margin:6px 0 0;color:rgba(255,255,255,.75);font-size:13px;}}
.body{{padding:28px 36px;font-size:15px;color:#1e293b;line-height:1.7;}}
.task-box{{background:#f8fafc;border:1.5px solid #e2e8f0;border-radius:12px;padding:16px 20px;margin:16px 0;}}
.task-box h3{{margin:0 0 10px;font-size:15px;font-weight:800;color:#1e3a5f;}}
.task-meta{{display:flex;flex-wrap:wrap;gap:12px;font-size:13px;color:#475569;margin-top:8px;}}
.tag{{background:#e0f2fe;color:#0369a1;border-radius:6px;padding:2px 9px;font-weight:700;font-size:12px;}}
.tag-overdue{{background:#fee2e2;color:#991b1b;}}
.tag-priority-critical,.tag-priority-urgent{{background:#fee2e2;color:#991b1b;}}
.tag-priority-high{{background:#fef3c7;color:#92400e;}}
.tag-priority-medium{{background:#dbeafe;color:#1d4ed8;}}
.btn{{display:inline-block;background:#0284c7;color:#fff;padding:11px 26px;border-radius:10px;text-decoration:none;font-weight:700;}}
.footer{{background:#f8fafc;padding:16px 36px;border-top:1px solid #e2e8f0;text-align:center;font-size:12px;color:#94a3b8;}}
</style></head>
<body>
<div class="w">
  <div class="hdr">
    <h1>📋 Task Follow-Up Notice</h1>
    <p>{org_name} — Office Administration</p>
  </div>
  <div class="body">
    <p>Dear <strong>{rec_name}</strong>,</p>
    <p><strong>{sender}</strong> ({org_name}) is following up on the status of the task assigned to
    <strong>{task.assigned_to.full_name}</strong>.</p>

    <div class="task-box">
      <h3>{task.title}</h3>
      <div class="task-meta">
        <span class="tag tag-priority-{task.priority}">Priority: {task.get_priority_display()}</span>
        <span class="tag {'tag-overdue' if overdue_days else ''}">Status: {task.get_status_display()}</span>
        {'<span class="tag tag-overdue">⚠ ' + str(overdue_days) + ' days overdue</span>' if overdue_days else ''}
      </div>
      <div style="margin-top:10px;font-size:13px;color:#64748b">
        <strong>Assigned to:</strong> {task.assigned_to.full_name}<br>
        <strong>Due:</strong> {due_str}<br>
        {'<strong>Project:</strong> ' + task.project.name + '<br>' if task.project else ''}
        <strong>Progress:</strong> {task.progress_pct}%
      </div>
    </div>

    {overdue_banner}
    {note_block}

    <p>Please ensure that <strong>{task.assigned_to.full_name}</strong> is aware of this task and
    provide an update on the expected completion timeline.</p>

    <div style="text-align:center;margin:24px 0">
      <a href="{task_url}" class="btn">View Task Details →</a>
    </div>
  </div>
  <div class="footer">{org_name} · Office Administration · This is an official follow-up notice.</div>
</div>
</body></html>"""

        try:
            msg = EmailMessage(
                subject=f'Follow-Up Required: "{task.title}" | {org_name}',
                body=html,
                from_email=org_email,
                to=[recipient_user.email],
                cc=[request.user.email] if request.user.email else [],
            )
            msg.content_subtype = 'html'
            msg.send()

            try:
                from apps.core.models import CoreNotification
                CoreNotification.objects.create(
                    recipient=recipient_user,
                    sender=request.user,
                    notification_type='task',
                    title=f'Follow-up: {task.title}',
                    message=f'{sender} is requesting a status update on this task.',
                    link=f'/tasks/{task.pk}/',
                )
            except Exception:
                pass

            messages.success(request,
                f'Follow-up sent to {rec_name} ({recipient_user.email}).')
        except Exception as e:
            messages.error(request, f'Failed to send email: {e}')

        return redirect(request.META.get('HTTP_REFERER', 'admin_dashboard'))