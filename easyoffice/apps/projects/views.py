from django.contrib.auth.mixins import LoginRequiredMixin
from django.views.generic import TemplateView, DetailView, View
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.db.models import Q, Count
from django.utils import timezone
from apps.projects.models import (
    Project, Milestone, ProjectUpdate, Risk,
    Survey, SurveyQuestion, SurveyResponse, SurveyAnswer,
    TrackingSheet, TrackingRow, ProjectLocation,
)
import json
from collections import Counter
from apps.core.models import User
from django.db.models import Sum
from apps.finance.models import PurchaseRequest, Payment, EmployeeFinanceRequest
import io
from datetime import date, timedelta
from django.http import HttpResponse, HttpResponseForbidden

def _is_project_member(user, project):
    return (
        user.is_superuser or
        project.project_manager == user or
        project.team_members.filter(id=user.id).exists()
    )


# ---------------------------------------------------------------------------
# Project List
# ---------------------------------------------------------------------------

class ProjectListView(LoginRequiredMixin, TemplateView):
    template_name = 'projects/project_list.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        user = self.request.user
        view = self.request.GET.get('view', 'my')
        q = self.request.GET.get('q', '')
        status = self.request.GET.get('status', '')
        priority = self.request.GET.get('priority', '')

        if user.is_superuser or view == 'all':
            projects = Project.objects.filter(
                Q(is_public=True) | Q(team_members=user) | Q(project_manager=user)
            ).distinct()
        else:
            projects = Project.objects.filter(
                Q(team_members=user) | Q(project_manager=user)
            ).distinct()

        if q:
            projects = projects.filter(
                Q(name__icontains=q) | Q(code__icontains=q) | Q(description__icontains=q)
            )
        if status:
            projects = projects.filter(status=status)
        if priority:
            projects = projects.filter(priority=priority)

        projects = projects.select_related('project_manager', 'department').order_by('-updated_at')

        my_projects = Project.objects.filter(
            Q(team_members=user) | Q(project_manager=user)
        ).distinct()

        ctx.update({
            'projects': projects,
            'status_choices': Project.Status.choices,
            'priority_choices': Project.Priority.choices,
            'view': view,
            'q': q,
            'status_filter': status,
            'priority_filter': priority,
            'total_count': projects.count(),
            'active_count': my_projects.filter(status='active').count(),
            'completed_count': my_projects.filter(status='completed').count(),
            'my_count': my_projects.count(),
        })
        return ctx


# ---------------------------------------------------------------------------
# Project Detail
# ---------------------------------------------------------------------------

class ProjectDetailView(LoginRequiredMixin, DetailView):
    model = Project
    template_name = 'projects/project_detail.html'
    context_object_name = 'project'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        project = self.object
        user = self.request.user

        milestones = project.milestones.all().order_by('order', 'due_date')
        tasks = project.tasks.select_related('assigned_to').order_by('status', '-created_at')[:15]
        updates = project.updates.select_related('author').order_by('-created_at')[:15]
        risks = project.risks.select_related('owner')
        team = project.team_members.filter(is_active=True).select_related(
            'staffprofile', 'staffprofile__position'
        )

        total_ms = milestones.count()
        completed_ms = milestones.filter(status='completed').count()
        overdue_ms = milestones.filter(
            status__in=['pending', 'in_progress'],
            due_date__lt=timezone.now().date()
        ).count()

        budget_pct = 0
        if project.budget and project.budget > 0:
            budget_pct = min(100, int((project.budget_spent / project.budget) * 100))

        days_remaining = None
        days_remaining_abs = None
        is_overdue = False
        if project.end_date:
            delta = project.end_date - timezone.now().date()
            days_remaining = delta.days
            is_overdue = days_remaining < 0 and project.status not in ('completed', 'cancelled')
            days_remaining_abs = abs(days_remaining)

        project_tags = []
        if project.tags:
            project_tags = [tag.strip() for tag in project.tags.split(",") if tag.strip()]

        # ── Linked meetings ─────────────────────────────────────────────
        try:
            from apps.meetings.models import Meeting
            project_meetings = Meeting.objects.filter(project=project).select_related(
                'organizer', 'project'
            ).order_by('-start_datetime')
            upcoming_meetings = project_meetings.filter(
                start_datetime__gte=timezone.now()
            ).order_by('start_datetime')[:5]
            recent_meetings = project_meetings[:5]
            meeting_count = project_meetings.count()
        except Exception:
            project_meetings = []
            upcoming_meetings = []
            recent_meetings = []
            meeting_count = 0

        # ── Linked finance data ─────────────────────────────────────────
        purchase_requests = project.purchase_requests.select_related(
            'requested_by', 'budget', 'approved_by'
        ).order_by('-created_at')[:8]

        try:
            from apps.finance.models import Payment
            project_payments = Payment.objects.filter(project=project).select_related(
                'paid_by', 'budget', 'purchase_request'
            ).order_by('-payment_date', '-created_at')[:8]
            finance_totals = Payment.objects.filter(project=project).aggregate(
                total_spent=Sum('amount')
            )
        except Exception:
            project_payments = []
            finance_totals = {'total_spent': 0}

        try:
            from apps.finance.models import EmployeeFinanceRequest
            employee_finance_requests = EmployeeFinanceRequest.objects.filter(
                project=project
            ).select_related('employee', 'budget', 'approved_by').order_by('-created_at')[:8]
        except Exception:
            employee_finance_requests = []

        total_purchase_est = purchase_requests.aggregate(t=Sum('estimated_cost'))['t'] if purchase_requests else 0
        finance_totals['total_purchase_est'] = total_purchase_est or 0

        ctx.update({
            'milestones': milestones,
            'updates': updates,
            'risks': risks,
            'active_risks': risks.filter(is_resolved=False),
            'resolved_risks': risks.filter(is_resolved=True),
            'tasks': tasks,
            'team_members': team,
            'is_member': _is_project_member(user, project),
            'is_manager': user.is_superuser or project.project_manager == user,

            'total_ms': total_ms,
            'completed_ms': completed_ms,
            'overdue_ms': overdue_ms,
            'budget_pct': budget_pct,
            'days_remaining': days_remaining,
            'days_remaining_abs': days_remaining_abs,
            'is_overdue': is_overdue,
            'project_tags': project_tags,

            'status_choices': Project.Status.choices,
            'risk_levels': Risk.Level.choices,
            'milestone_statuses': Milestone.Status.choices,
            'staff_list': User.objects.filter(status='active', is_active=True).order_by('first_name'),

            # finance
            'purchase_requests': purchase_requests,
            'project_payments': project_payments,
            'employee_finance_requests': employee_finance_requests,
            'finance_totals': finance_totals,

            # meetings
            'project_meetings': project_meetings,
            'upcoming_meetings': upcoming_meetings,
            'recent_meetings': recent_meetings,
            'meeting_count': meeting_count,
        })
        return ctx


# ---------------------------------------------------------------------------
# Project Create
# ---------------------------------------------------------------------------

class ProjectCreateView(LoginRequiredMixin, View):
    template_name = 'projects/project_form.html'

    def get(self, request):
        from apps.organization.models import Department, Unit
        return render(request, self.template_name, {
            'departments': Department.objects.filter(is_active=True),
            'units': Unit.objects.filter(is_active=True),
            'staff_list': User.objects.filter(status='active', is_active=True).order_by('first_name'),
            'priority_choices': Project.Priority.choices,
            'status_choices': Project.Status.choices,
            'mode': 'create',
        })

    def post(self, request):
        try:
            import shortuuid
            code = request.POST.get('code', '').strip() or shortuuid.ShortUUID().random(length=6).upper()
        except ImportError:
            import random, string
            code = request.POST.get('code', '').strip() or ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))

        project = Project.objects.create(
            name=request.POST['name'],
            code=code,
            description=request.POST.get('description', ''),
            project_manager_id=request.POST.get('project_manager') or request.user.id,
            priority=request.POST.get('priority', 'medium'),
            status=request.POST.get('status', 'planning'),
            start_date=request.POST.get('start_date') or None,
            end_date=request.POST.get('end_date') or None,
            budget=request.POST.get('budget') or None,
            cover_color=request.POST.get('cover_color', '#1e3a5f'),
            tags=request.POST.get('tags', ''),
            is_public=request.POST.get('is_public') == 'on',
        )
        member_ids = request.POST.getlist('team_members')
        if member_ids:
            project.team_members.set(User.objects.filter(id__in=member_ids))
        if request.POST.get('dept_id'):
            try:
                from apps.organization.models import Department
                project.department = Department.objects.get(id=request.POST['dept_id'])
                project.save(update_fields=['department'])
            except Exception:
                pass

        messages.success(request, f'Project "{project.name}" created.')
        return redirect('project_detail', pk=project.id)


# ---------------------------------------------------------------------------
# Project Edit
# ---------------------------------------------------------------------------

class ProjectEditView(LoginRequiredMixin, View):
    template_name = 'projects/project_form.html'

    def get(self, request, pk):
        project = get_object_or_404(Project, pk=pk)
        if not _is_project_member(request.user, project):
            messages.error(request, 'You do not have permission to edit this project.')
            return redirect('project_detail', pk=pk)
        from apps.organization.models import Department, Unit
        return render(request, self.template_name, {
            'project': project,
            'departments': Department.objects.filter(is_active=True),
            'units': Unit.objects.filter(is_active=True),
            'staff_list': User.objects.filter(status='active', is_active=True).order_by('first_name'),
            'priority_choices': Project.Priority.choices,
            'status_choices': Project.Status.choices,
            'mode': 'edit',
        })

    def post(self, request, pk):
        project = get_object_or_404(Project, pk=pk)
        if not _is_project_member(request.user, project):
            messages.error(request, 'Permission denied.')
            return redirect('project_detail', pk=pk)

        project.name = request.POST.get('name', project.name)
        project.description = request.POST.get('description', project.description)
        project.priority = request.POST.get('priority', project.priority)
        project.status = request.POST.get('status', project.status)
        project.start_date = request.POST.get('start_date') or None
        project.end_date = request.POST.get('end_date') or None
        project.budget = request.POST.get('budget') or None
        project.cover_color = request.POST.get('cover_color', project.cover_color)
        project.tags = request.POST.get('tags', project.tags)
        project.is_public = request.POST.get('is_public') == 'on'
        pm_id = request.POST.get('project_manager')
        if pm_id:
            try:
                project.project_manager = User.objects.get(id=pm_id)
            except User.DoesNotExist:
                pass
        if request.POST.get('dept_id'):
            try:
                from apps.organization.models import Department
                project.department = Department.objects.get(id=request.POST['dept_id'])
            except Exception:
                pass
        project.save()

        member_ids = request.POST.getlist('team_members')
        if member_ids is not None:
            project.team_members.set(User.objects.filter(id__in=member_ids))

        messages.success(request, f'Project "{project.name}" updated.')
        return redirect('project_detail', pk=pk)


# ---------------------------------------------------------------------------
# Post Update / Change Status + Progress
# ---------------------------------------------------------------------------

class ProjectUpdateStatusView(LoginRequiredMixin, View):
    def post(self, request, pk):
        project = get_object_or_404(Project, pk=pk)
        if not _is_project_member(request.user, project):
            messages.error(request, 'Permission denied.')
            return redirect('project_detail', pk=pk)

        content = request.POST.get('content', '').strip()
        new_status = request.POST.get('status', project.status)
        try:
            progress = max(0, min(100, int(request.POST.get('progress_pct', project.progress_pct))))
        except (ValueError, TypeError):
            progress = project.progress_pct

        if content:
            ProjectUpdate.objects.create(
                project=project,
                author=request.user,
                title=request.POST.get('title', ''),
                content=content,
                progress_at_time=progress,
                status_at_time=new_status,
            )

        project.status = new_status
        project.progress_pct = progress
        if new_status == 'completed' and not project.completed_at:
            project.completed_at = timezone.now()
        project.save(update_fields=['status', 'progress_pct', 'completed_at', 'updated_at'])
        messages.success(request, 'Project updated.')
        return redirect('project_detail', pk=pk)


# ---------------------------------------------------------------------------
# Add Milestone
# ---------------------------------------------------------------------------

class ProjectMilestoneView(LoginRequiredMixin, View):
    def post(self, request, pk):
        project = get_object_or_404(Project, pk=pk)
        if not _is_project_member(request.user, project):
            messages.error(request, 'Permission denied.')
            return redirect('project_detail', pk=pk)

        Milestone.objects.create(
            project=project,
            name=request.POST['name'],
            description=request.POST.get('description', ''),
            due_date=request.POST['due_date'],
            order=project.milestones.count() + 1,
        )
        messages.success(request, 'Milestone added.')
        return redirect('project_detail', pk=pk)


# ---------------------------------------------------------------------------
# Update Milestone Status
# ---------------------------------------------------------------------------

class MilestoneStatusView(LoginRequiredMixin, View):
    def post(self, request, pk, mid):
        project = get_object_or_404(Project, pk=pk)
        milestone = get_object_or_404(Milestone, pk=mid, project=project)
        if not _is_project_member(request.user, project):
            messages.error(request, 'Permission denied.')
            return redirect('project_detail', pk=pk)

        new_status = request.POST.get('status', milestone.status)
        milestone.status = new_status
        if new_status == 'completed' and not milestone.completed_at:
            milestone.completed_at = timezone.now()
        milestone.save()
        messages.success(request, f'Milestone "{milestone.name}" updated.')
        return redirect('project_detail', pk=pk)


# ---------------------------------------------------------------------------
# Risks
# ---------------------------------------------------------------------------

class ProjectRiskView(LoginRequiredMixin, View):
    def post(self, request, pk):
        project = get_object_or_404(Project, pk=pk)
        if not _is_project_member(request.user, project):
            messages.error(request, 'Permission denied.')
            return redirect('project_detail', pk=pk)

        action = request.POST.get('action', 'add')

        if action == 'resolve':
            risk = get_object_or_404(Risk, pk=request.POST.get('risk_id'), project=project)
            risk.is_resolved = True
            risk.save(update_fields=['is_resolved'])
            messages.success(request, f'Risk "{risk.title}" marked as resolved.')

        elif action == 'add':
            owner_id = request.POST.get('owner_id')
            Risk.objects.create(
                project=project,
                title=request.POST['title'],
                description=request.POST.get('description', ''),
                level=request.POST.get('level', 'medium'),
                mitigation=request.POST.get('mitigation', ''),
                owner_id=owner_id or None,
            )
            messages.success(request, 'Risk logged.')

        return redirect('project_detail', pk=pk)


class ProjectGanttPDFView(LoginRequiredMixin, View):
    """
    Generate and download a Gantt chart PDF for a project.
    The chart shows the project span and all milestones on a landscape A4 page.
    Requires: pip install reportlab
    """

    def get(self, request, pk):
        from apps.projects.models import Project

        project = get_object_or_404(Project, pk=pk)

        # Access control: only project members / public projects
        is_member = (
                project.is_public
                or project.project_manager == request.user
                or project.team_members.filter(pk=request.user.pk).exists()
                or request.user.is_superuser
        )
        if not is_member:
            return HttpResponseForbidden("You do not have access to this project.")

        milestones = list(project.milestones.all().order_by('order', 'due_date'))
        pdf_bytes = _build_gantt_pdf(project, milestones)

        filename = f"{project.code}-gantt.pdf"
        response = HttpResponse(pdf_bytes, content_type='application/pdf')
        response['Content-Disposition'] = f'attachment; filename="{filename}"'
        return response


# ─────────────────────────────────────────────────────────────────────────────
# _build_gantt_pdf helper  (place near the other helpers at the top of views.py)
# ─────────────────────────────────────────────────────────────────────────────

def _build_gantt_pdf(project, milestones):
    """
    Build a landscape A4 Gantt chart PDF using reportlab.
    Returns raw PDF bytes.
    """
    try:
        from reportlab.pdfgen import canvas as rl_canvas
        from reportlab.lib.pagesizes import A4, landscape
        from reportlab.lib import colors
        from reportlab.lib.units import mm
    except ImportError:
        raise RuntimeError("reportlab is not installed. Run: pip install reportlab")

    buf = io.BytesIO()

    PAGE_W, PAGE_H = landscape(A4)  # ~842 × 595 pt
    MARGIN = 18 * mm

    # ── Layout constants ──────────────────────────────────────────────────────
    LABEL_W = 148  # left column (milestone names)
    CHART_X = MARGIN + LABEL_W + 6  # x start of the timeline area
    CHART_W = PAGE_W - CHART_X - MARGIN  # timeline width
    CHART_TOP = PAGE_H - MARGIN - 50  # y of the top edge (reportlab: bottom-left origin)

    TITLE_H = 28
    META_H = 16
    HDR_H = 22  # month strip height
    PROJ_H = 16  # project bar row height
    ROW_H = 24  # each milestone row height
    LEGEND_H = 14

    today = date.today()

    # ── Determine timeline range ───────────────────────────────────────────────
    ms_dates = [m.due_date for m in milestones if m.due_date]

    t_min = project.start_date or (min(ms_dates) if ms_dates else today - timedelta(days=30))
    t_max = project.end_date or (max(ms_dates) if ms_dates else today + timedelta(days=90))

    # Extend range to include today + 1-week padding on each side
    t_min = date(t_min.year, t_min.month, 1)
    t_max_padded = t_max + timedelta(days=14)
    t_max_month_end = date(
        t_max_padded.year,
        t_max_padded.month,
        1
    )
    # Go to start of following month for a clean right edge
    if t_max_month_end.month == 12:
        t_max = date(t_max_month_end.year + 1, 1, 1)
    else:
        t_max = date(t_max_month_end.year, t_max_month_end.month + 1, 1)

    total_days = max(1, (t_max - t_min).days)

    def x_of(d):
        """X coordinate for a date."""
        return CHART_X + (d - t_min).days / total_days * CHART_W

    # ── Colours ───────────────────────────────────────────────────────────────
    C_COVER = colors.HexColor(project.cover_color or '#1e3a5f')
    C_PENDING = colors.HexColor('#94a3b8')
    C_IN_PROG = colors.HexColor('#3b82f6')
    C_DONE = colors.HexColor('#10b981')
    C_MISSED = colors.HexColor('#ef4444')
    C_TODAY = colors.HexColor('#f59e0b')
    C_GRID = colors.HexColor('#e2e8f0')
    C_BG_ALT = colors.HexColor('#f8fafc')
    C_TEXT = colors.HexColor('#1e293b')
    C_MUTED = colors.HexColor('#64748b')
    C_WHITE = colors.white
    C_LABEL_COL = colors.HexColor('#f1f5f9')  # left column background

    STATUS_COLOR = {
        'pending': C_PENDING,
        'in_progress': C_IN_PROG,
        'completed': C_DONE,
        'missed': C_MISSED,
    }

    c = rl_canvas.Canvas(buf, pagesize=landscape(A4))
    c.setTitle(f"{project.name} — Gantt Chart")

    # ── Total chart height calc ───────────────────────────────────────────────
    n_rows = len(milestones)
    chart_h = HDR_H + PROJ_H + n_rows * ROW_H  # just the rows

    # y_top: top of the Gantt rows block (not title)
    title_block = TITLE_H + META_H + 6
    y_top = PAGE_H - MARGIN - title_block - 4  # top of month header

    # ── Title block ──────────────────────────────────────────────────────────
    # Colour bar at very top
    c.setFillColor(C_COVER)
    c.rect(MARGIN, PAGE_H - MARGIN - TITLE_H, PAGE_W - 2 * MARGIN, TITLE_H, stroke=0, fill=1)

    c.setFillColor(C_WHITE)
    c.setFont('Helvetica-Bold', 13)
    c.drawString(MARGIN + 8, PAGE_H - MARGIN - TITLE_H + 8, f"{project.name}  —  Gantt Chart")

    c.setFont('Helvetica', 8)
    c.setFillColor(colors.HexColor('#dbeafe'))
    right_title = f"{project.code}  |  {project.get_status_display()}  |  Progress: {project.progress_pct}%"
    c.drawRightString(PAGE_W - MARGIN - 8, PAGE_H - MARGIN - TITLE_H + 8, right_title)

    # Meta line
    c.setFillColor(C_MUTED)
    c.setFont('Helvetica', 8)
    meta_y = PAGE_H - MARGIN - TITLE_H - META_H + 2
    start_str = project.start_date.strftime('%d %b %Y') if project.start_date else 'TBD'
    end_str = project.end_date.strftime('%d %b %Y') if project.end_date else 'TBD'
    meta_txt = f"Start: {start_str}   End: {end_str}   Milestones: {n_rows}   Generated: {today.strftime('%d %b %Y')}"
    c.drawString(MARGIN, meta_y, meta_txt)

    # Separator
    c.setStrokeColor(C_GRID)
    c.setLineWidth(0.5)
    c.line(MARGIN, meta_y - 3, PAGE_W - MARGIN, meta_y - 3)

    # ── Month header row ──────────────────────────────────────────────────────
    hdr_y = y_top  # bottom of header row
    hdr_top = y_top + HDR_H  # top of header row

    c.setFillColor(C_LABEL_COL)
    c.rect(MARGIN, hdr_y, LABEL_W + 6, HDR_H, stroke=0, fill=1)
    c.setFillColor(C_MUTED)
    c.setFont('Helvetica-Bold', 7)
    c.drawString(MARGIN + 4, hdr_y + HDR_H / 2 - 3, "MILESTONE")

    # Month columns
    cur = date(t_min.year, t_min.month, 1)
    while cur < t_max:
        mx = x_of(cur)
        # Vertical grid line
        c.setStrokeColor(C_GRID)
        c.setLineWidth(0.4)
        c.line(mx, MARGIN, mx, hdr_top)

        # Month label in header
        c.setFillColor(C_MUTED)
        c.setFont('Helvetica-Bold', 7)
        label = cur.strftime('%b %Y')
        c.drawString(mx + 3, hdr_y + 7, label)

        # Advance to next month
        if cur.month == 12:
            cur = date(cur.year + 1, 1, 1)
        else:
            cur = date(cur.year, cur.month + 1, 1)

    # Header bottom border
    c.setStrokeColor(C_MUTED)
    c.setLineWidth(0.8)
    c.line(MARGIN, hdr_y, PAGE_W - MARGIN, hdr_y)

    # ── Project span row ──────────────────────────────────────────────────────
    proj_y = hdr_y - PROJ_H  # bottom of this row

    # Row background
    c.setFillColor(colors.HexColor('#f0f4ff'))
    c.rect(MARGIN, proj_y, PAGE_W - 2 * MARGIN, PROJ_H, stroke=0, fill=1)

    # Row label
    c.setFillColor(C_TEXT)
    c.setFont('Helvetica-Bold', 7.5)
    c.drawString(MARGIN + 4, proj_y + PROJ_H / 2 - 3, "Project Span")

    # Project bar
    if project.start_date and project.end_date:
        ps_x = x_of(project.start_date)
        pe_x = x_of(project.end_date)
        bar_w = max(2, pe_x - ps_x)
        bar_y = proj_y + 4
        bar_h = PROJ_H - 8

        # Track
        c.setFillColor(colors.HexColor('#dbeafe'))
        c.roundRect(ps_x, bar_y, bar_w, bar_h, 3, stroke=0, fill=1)

        # Progress fill
        prog_w = max(0, bar_w * project.progress_pct / 100)
        if prog_w > 0:
            c.setFillColor(C_COVER)
            c.roundRect(ps_x, bar_y, prog_w, bar_h, 3, stroke=0, fill=1)

        # Progress label
        c.setFillColor(C_COVER)
        c.setFont('Helvetica-Bold', 7)
        c.drawString(ps_x + 4, bar_y + bar_h / 2 - 3, f"{project.progress_pct}%")

    # Row border
    c.setStrokeColor(C_GRID)
    c.setLineWidth(0.5)
    c.line(MARGIN, proj_y, PAGE_W - MARGIN, proj_y)

    # ── Milestone rows ────────────────────────────────────────────────────────
    for idx, ms in enumerate(milestones):
        row_y = proj_y - (idx + 1) * ROW_H  # bottom of this row
        sc = STATUS_COLOR.get(ms.status, C_PENDING)

        # Alternating background
        if idx % 2 == 1:
            c.setFillColor(C_BG_ALT)
            c.rect(MARGIN, row_y, PAGE_W - 2 * MARGIN, ROW_H, stroke=0, fill=1)

        # ── Label column ─────────────────────────────────────────────────────
        # Number badge
        badge_x = MARGIN + 4
        badge_y = row_y + (ROW_H - 10) / 2
        c.setFillColor(sc)
        c.roundRect(badge_x, badge_y, 12, 10, 2, stroke=0, fill=1)
        c.setFillColor(C_WHITE)
        c.setFont('Helvetica-Bold', 6)
        c.drawCentredString(badge_x + 6, badge_y + 2.5, str(idx + 1))

        # Milestone name (truncate to fit)
        max_chars = 22
        name_disp = ms.name if len(ms.name) <= max_chars else ms.name[:max_chars] + '…'
        c.setFillColor(C_TEXT)
        c.setFont('Helvetica-Bold', 7.5)
        c.drawString(badge_x + 16, row_y + ROW_H / 2 + 1, name_disp)

        # Status text
        status_labels = {
            'pending': 'Pending', 'in_progress': 'In Progress',
            'completed': 'Completed', 'missed': 'Missed'
        }
        c.setFillColor(sc)
        c.setFont('Helvetica', 6.5)
        c.drawString(badge_x + 16, row_y + ROW_H / 2 - 7, status_labels.get(ms.status, ''))

        # ── Timeline area ─────────────────────────────────────────────────────
        if not ms.due_date:
            c.setStrokeColor(C_GRID)
            c.setLineWidth(0.3)
            c.line(MARGIN, row_y, PAGE_W - MARGIN, row_y)
            continue

        due_x = x_of(ms.due_date)
        bar_y = row_y + (ROW_H - 6) / 2
        bar_h = 6
        track_x = CHART_X + 2

        # Full grey track
        c.setFillColor(C_GRID)
        c.roundRect(track_x, bar_y, CHART_W - 4, bar_h, 3, stroke=0, fill=1)

        # Coloured fill up to due date
        fill_w = max(0, due_x - track_x - 8)
        if fill_w > 0:
            sc_light = colors.HexColor(
                {'pending': '#e2e8f0', 'in_progress': '#dbeafe',
                 'completed': '#d1fae5', 'missed': '#fee2e2'}.get(ms.status, '#e2e8f0')
            )
            c.setFillColor(sc_light)
            c.roundRect(track_x, bar_y, fill_w, bar_h, 3, stroke=0, fill=1)

        # Diamond marker at due date
        d_cx = due_x
        d_cy = row_y + ROW_H / 2
        d_sz = 5.5
        from reportlab.graphics.shapes import Polygon
        # Draw diamond using lines
        c.setFillColor(sc)
        c.setStrokeColor(C_WHITE)
        c.setLineWidth(1)
        path = c.beginPath()
        path.moveTo(d_cx, d_cy + d_sz)
        path.lineTo(d_cx + d_sz, d_cy)
        path.lineTo(d_cx, d_cy - d_sz)
        path.lineTo(d_cx - d_sz, d_cy)
        path.close()
        c.drawPath(path, fill=1, stroke=1)

        # Status icon inside diamond
        icon = {'completed': '✓', 'missed': '✕', 'in_progress': '▶'}.get(ms.status, '·')
        c.setFillColor(C_WHITE)
        c.setFont('Helvetica-Bold', 5)
        c.drawCentredString(d_cx, d_cy - 1.8, icon)

        # Due-date label above diamond
        due_label = ms.due_date.strftime('%d %b')
        c.setFillColor(sc)
        c.setFont('Helvetica-Bold', 6.5)
        c.drawCentredString(d_cx, d_cy + d_sz + 3, due_label)

        # Row separator
        c.setStrokeColor(C_GRID)
        c.setLineWidth(0.3)
        c.line(MARGIN, row_y, PAGE_W - MARGIN, row_y)

    # ── Today line ────────────────────────────────────────────────────────────
    today_x = x_of(today)
    chart_bottom = proj_y - n_rows * ROW_H
    if CHART_X <= today_x <= CHART_X + CHART_W:
        c.setStrokeColor(C_TODAY)
        c.setLineWidth(1.2)
        c.setDash(4, 3)
        c.line(today_x, chart_bottom, today_x, hdr_top)
        c.setDash()  # reset dash

        # "TODAY" badge at top of line
        badge_w, badge_h = 28, 10
        c.setFillColor(C_TODAY)
        c.roundRect(today_x - badge_w / 2, hdr_top, badge_w, badge_h, 3, stroke=0, fill=1)
        c.setFillColor(C_WHITE)
        c.setFont('Helvetica-Bold', 6)
        c.drawCentredString(today_x, hdr_top + 3, 'TODAY')

    # ── Column separator ──────────────────────────────────────────────────────
    sep_x = CHART_X - 2
    c.setStrokeColor(colors.HexColor('#cbd5e1'))
    c.setLineWidth(1)
    c.line(sep_x, chart_bottom, sep_x, hdr_top)

    # Outer border
    c.setStrokeColor(C_GRID)
    c.setLineWidth(0.8)
    c.rect(MARGIN, chart_bottom, PAGE_W - 2 * MARGIN, hdr_top - chart_bottom, stroke=1, fill=0)

    # ── Legend strip at bottom of page ────────────────────────────────────────
    legend_y = MARGIN + 2
    c.setFont('Helvetica-Bold', 6.5)
    c.setFillColor(C_MUTED)
    c.drawString(MARGIN, legend_y, 'Legend:')
    lx = MARGIN + 36
    for lbl, col in [('Pending', C_PENDING), ('In Progress', C_IN_PROG),
                     ('Completed', C_DONE), ('Missed', C_MISSED),
                     ('Today', C_TODAY)]:
        c.setFillColor(col)
        c.circle(lx + 4, legend_y + 3, 3.5, fill=1, stroke=0)
        c.setFillColor(C_MUTED)
        c.setFont('Helvetica', 6.5)
        c.drawString(lx + 10, legend_y, lbl)
        lx += c.stringWidth(lbl, 'Helvetica', 6.5) + 22

    c.save()
    return buf.getvalue()


# =============================================================================
# SURVEY VIEWS
# =============================================================================

class ProjectSurveyListView(LoginRequiredMixin, View):
    """List all surveys for a project + create new surveys."""

    def get(self, request, pk):
        project = get_object_or_404(Project, pk=pk)
        surveys = project.surveys.select_related('created_by').annotate(
            resp_count=Count('responses')
        ).order_by('-created_at')
        return render(request, 'projects/survey_list.html', {
            'project': project,
            'surveys': surveys,
            'is_member': _is_project_member(request.user, project),
            'is_manager': request.user.is_superuser or project.project_manager == request.user,
            'question_types': SurveyQuestion.Type.choices,
        })

    def post(self, request, pk):
        project = get_object_or_404(Project, pk=pk)
        if not _is_project_member(request.user, project):
            return HttpResponseForbidden()

        title = request.POST.get('title', '').strip()
        if not title:
            messages.error(request, 'Survey title is required.')
            return redirect('project_surveys', pk=pk)

        survey = Survey.objects.create(
            project=project,
            title=title,
            description=request.POST.get('description', '').strip(),
            is_anonymous=request.POST.get('is_anonymous') == 'on',
            closes_at=request.POST.get('closes_at') or None,
            created_by=request.user,
        )

        q_texts = request.POST.getlist('q_text')
        q_types = request.POST.getlist('q_type')
        q_options = request.POST.getlist('q_options')
        q_req = set(request.POST.getlist('q_required'))  # set of "0","1",…

        for i, (text, qtype, opts) in enumerate(zip(q_texts, q_types, q_options)):
            if not text.strip():
                continue
            SurveyQuestion.objects.create(
                survey=survey,
                text=text.strip(),
                q_type=qtype,
                options=opts,
                is_required=(str(i) in q_req),
                order=i,
            )

        messages.success(request, f'Survey "{title}" created with {survey.questions.count()} question(s).')
        return redirect('survey_detail', pk=pk, sid=survey.pk)


class SurveyDetailView(LoginRequiredMixin, View):
    """Results dashboard. Redirects non-members to submit page."""

    def get(self, request, pk, sid):
        project = get_object_or_404(Project, pk=pk)
        survey = get_object_or_404(Survey, pk=sid, project=project)
        is_member = _is_project_member(request.user, project)
        is_manager = request.user.is_superuser or project.project_manager == request.user

        if not is_member:
            return redirect('survey_submit', pk=pk, sid=sid)

        questions = survey.questions.order_by('order')
        responses = (survey.responses
                     .select_related('respondent')
                     .prefetch_related('answers__question')
                     .order_by('-submitted_at'))

        already_responded = (
                not survey.is_anonymous
                and responses.filter(respondent=request.user).exists()
        )

        # ── Pre-process per-question results so templates need no custom filters ──
        question_results = []
        for q in questions:
            raw_answers = list(
                SurveyAnswer.objects.filter(question=q).values_list('value', flat=True)
            )
            result = {'question': q, 'raw': raw_answers, 'count': len(raw_answers)}

            if q.q_type in ('single_choice', 'multi_choice'):
                all_vals = []
                for a in raw_answers:
                    all_vals.extend([v.strip() for v in a.split(',') if v.strip()])
                tally = dict(Counter(all_vals))
                total = sum(tally.values()) or 1
                options = q.get_options_list()
                # Build ordered list safe for templates: [{label, count, pct}, ...]
                tally_list = []
                for opt in options:
                    cnt = tally.get(opt, 0)
                    tally_list.append({
                        'label': opt, 'count': cnt,
                        'pct': round(cnt / total * 100),
                    })
                # Any write-in answers not in the defined options
                for k, v in tally.items():
                    if k not in options:
                        tally_list.append({
                            'label': k, 'count': v,
                            'pct': round(v / total * 100), 'extra': True,
                        })
                result.update({
                    'tally_list': tally_list,
                    'total': sum(tally.values()),
                })

            elif q.q_type == 'rating':
                nums = [int(a) for a in raw_answers if a.strip().isdigit()]
                total = len(nums) or 1
                tally = dict(Counter(nums))
                result.update({
                    'avg': round(sum(nums) / len(nums), 1) if nums else None,
                    'rating_list': [
                        {'score': i, 'count': tally.get(i, 0),
                         'pct': round(tally.get(i, 0) / total * 100)}
                        for i in range(1, 6)
                    ],
                })

            elif q.q_type == 'yes_no':
                tally = dict(Counter(raw_answers))
                total = sum(tally.values()) or 1
                result.update({
                    'yn_list': [
                        {'label': 'Yes', 'count': tally.get('Yes', 0),
                         'pct': round(tally.get('Yes', 0) / total * 100), 'color': '#10b981'},
                        {'label': 'No', 'count': tally.get('No', 0),
                         'pct': round(tally.get('No', 0) / total * 100), 'color': '#f87171'},
                    ],
                    'total': total,
                })

            question_results.append(result)

        return render(request, 'projects/survey_detail.html', {
            'project': project,
            'survey': survey,
            'questions': questions,
            'responses': responses,
            'question_results': question_results,
            'is_manager': is_manager,
            'is_member': is_member,
            'already_responded': already_responded,
        })


class SurveySubmitView(LoginRequiredMixin, View):
    """Survey fill-in page for any authenticated user."""

    def _guard(self, request, survey, pk):
        """Return a redirect if the survey cannot accept this response, else None."""
        if not survey.is_active:
            messages.warning(request, 'This survey is no longer accepting responses.')
            return redirect('project_surveys', pk=pk)
        if not survey.is_anonymous and survey.responses.filter(respondent=request.user).exists():
            messages.info(request, 'You have already submitted a response to this survey.')
            return redirect('survey_detail', pk=pk, sid=survey.pk)
        return None

    def get(self, request, pk, sid):
        project = get_object_or_404(Project, pk=pk)
        survey = get_object_or_404(Survey, pk=sid, project=project)
        if (guard := self._guard(request, survey, pk)):
            return guard
        return render(request, 'projects/survey_submit.html', {
            'project': project,
            'survey': survey,
            'questions': survey.questions.order_by('order'),
        })

    def post(self, request, pk, sid):
        project = get_object_or_404(Project, pk=pk)
        survey = get_object_or_404(Survey, pk=sid, project=project)
        if (guard := self._guard(request, survey, pk)):
            return guard

        resp = SurveyResponse.objects.create(
            survey=survey,
            respondent=None if survey.is_anonymous else request.user,
            respondent_name=(
                request.POST.get('respondent_name', '').strip()
                if survey.is_anonymous
                else request.user.get_full_name()
            ),
        )

        for q in survey.questions.order_by('order'):
            if q.q_type == 'multi_choice':
                val = ', '.join(request.POST.getlist(f'q_{q.pk}'))
            else:
                val = request.POST.get(f'q_{q.pk}', '').strip()
            SurveyAnswer.objects.create(response=resp, question=q, value=val)

        messages.success(request, 'Response submitted — thank you!')
        return redirect('project_surveys', pk=pk)


class SurveyToggleView(LoginRequiredMixin, View):
    """Activate / close a survey (project manager only)."""

    def post(self, request, pk, sid):
        project = get_object_or_404(Project, pk=pk)
        if not (request.user.is_superuser or project.project_manager == request.user):
            return HttpResponseForbidden()
        survey = get_object_or_404(Survey, pk=sid, project=project)
        survey.is_active = not survey.is_active
        survey.save(update_fields=['is_active'])
        label = 'activated' if survey.is_active else 'closed'
        messages.success(request, f'Survey "{survey.title}" {label}.')
        return redirect('survey_detail', pk=pk, sid=sid)


class SurveyDeleteView(LoginRequiredMixin, View):
    def post(self, request, pk, sid):
        project = get_object_or_404(Project, pk=pk)
        if not (request.user.is_superuser or project.project_manager == request.user):
            return HttpResponseForbidden()
        survey = get_object_or_404(Survey, pk=sid, project=project)
        survey.delete()
        messages.success(request, 'Survey deleted.')
        return redirect('project_surveys', pk=pk)


# =============================================================================
# TRACKING SHEET VIEWS
# =============================================================================

class TrackingSheetView(LoginRequiredMixin, View):
    """Main tracking sheet (get-or-create per project)."""

    def get(self, request, pk):
        project = get_object_or_404(Project, pk=pk)
        sheet, _ = TrackingSheet.objects.get_or_create(
            project=project,
            defaults={'title': f'{project.name} Tracker', 'created_by': request.user}
        )
        columns = sheet.get_columns()
        rows_qs = sheet.rows.select_related('created_by').order_by('order', 'created_at')

        # Pre-process so templates can render cell values without custom filters.
        # Each item: {'row': TrackingRow, 'cells': [(label, type, key, value), ...], 'data_json': str}
        rows_processed = []
        for row in rows_qs:
            data = row.get_data()
            cells = [
                (col['label'], col['type'], col['key'], data.get(col['key'], ''))
                for col in columns
            ]
            rows_processed.append({
                'row': row,
                'cells': cells,
                'data_json': json.dumps(data),
            })

        return render(request, 'projects/tracking_sheet.html', {
            'project': project,
            'sheet': sheet,
            'columns': columns,
            'rows_processed': rows_processed,
            'row_count': len(rows_processed),
            'is_member': _is_project_member(request.user, project),
            'is_manager': request.user.is_superuser or project.project_manager == request.user,
        })

class TrackingSheetUpdateColumnsView(LoginRequiredMixin, View):
    """Save column definitions (manager only)."""

    def post(self, request, pk):
        project = get_object_or_404(Project, pk=pk)
        if not (request.user.is_superuser or project.project_manager == request.user):
            return HttpResponseForbidden()

        sheet, _ = TrackingSheet.objects.get_or_create(
            project=project, defaults={'created_by': request.user}
        )
        sheet.title = request.POST.get('sheet_title', sheet.title).strip() or sheet.title

        labels = request.POST.getlist('col_label')
        types = request.POST.getlist('col_type')
        cols = []
        for i, (label, ctype) in enumerate(zip(labels, types)):
            if label.strip():
                safe = label.strip().lower().replace(' ', '_')
                cols.append({'key': f'c{i}_{safe}', 'label': label.strip(), 'type': ctype})
        sheet.set_columns(cols)
        sheet.save()
        messages.success(request, 'Columns updated.')
        return redirect('project_tracking', pk=pk)


class TrackingRowAddView(LoginRequiredMixin, View):
    def post(self, request, pk):
        project = get_object_or_404(Project, pk=pk)
        if not _is_project_member(request.user, project):
            return HttpResponseForbidden()
        sheet, _ = TrackingSheet.objects.get_or_create(
            project=project, defaults={'created_by': request.user}
        )
        data = {col['key']: request.POST.get(f'data_{col["key"]}', '') for col in sheet.get_columns()}
        TrackingRow.objects.create(
            sheet=sheet,
            data_json=json.dumps(data),
            order=sheet.rows.count(),
            created_by=request.user,
        )
        messages.success(request, 'Row added.')
        return redirect('project_tracking', pk=pk)


class TrackingRowEditView(LoginRequiredMixin, View):
    def post(self, request, pk, rid):
        project = get_object_or_404(Project, pk=pk)
        if not _is_project_member(request.user, project):
            return HttpResponseForbidden()
        row = get_object_or_404(TrackingRow, pk=rid, sheet__project=project)
        data = {col['key']: request.POST.get(f'data_{col["key"]}', '') for col in row.sheet.get_columns()}
        row.data_json = json.dumps(data)
        row.save(update_fields=['data_json'])
        messages.success(request, 'Row updated.')
        return redirect('project_tracking', pk=pk)


class TrackingRowDeleteView(LoginRequiredMixin, View):
    def post(self, request, pk, rid):
        project = get_object_or_404(Project, pk=pk)
        if not _is_project_member(request.user, project):
            return HttpResponseForbidden()
        get_object_or_404(TrackingRow, pk=rid, sheet__project=project).delete()
        messages.success(request, 'Row deleted.')
        return redirect('project_tracking', pk=pk)


class TrackingSheetExportCSVView(LoginRequiredMixin, View):
    def get(self, request, pk):
        import csv as _csv
        project = get_object_or_404(Project, pk=pk)
        sheet = get_object_or_404(TrackingSheet, project=project)
        columns = sheet.get_columns()
        rows = sheet.rows.select_related('created_by').order_by('order', 'created_at')

        resp = HttpResponse(content_type='text/csv')
        resp['Content-Disposition'] = f'attachment; filename="{project.code}_tracking.csv"'
        writer = _csv.writer(resp)
        writer.writerow(['#'] + [c['label'] for c in columns] + ['Added By', 'Date'])

        for i, row in enumerate(rows, 1):
            d = row.get_data()
            writer.writerow(
                [i]
                + [d.get(c['key'], '') for c in columns]
                + [
                    row.created_by.get_full_name() if row.created_by else '',
                    row.created_at.strftime('%Y-%m-%d'),
                ]
            )
        return resp


# =============================================================================
# LOCATION MAP VIEWS
# =============================================================================

class ProjectLocationMapView(LoginRequiredMixin, View):
    def get(self, request, pk):
        project = get_object_or_404(Project, pk=pk)
        locations = project.locations.select_related('assigned_to').order_by('name')
        return render(request, 'projects/location_map.html', {
            'project': project,
            'locations': locations,
            'location_count': locations.count(),
            'is_member': _is_project_member(request.user, project),
            'is_manager': request.user.is_superuser or project.project_manager == request.user,
            'status_choices': ProjectLocation.Status.choices,
            'staff_list': User.objects.filter(is_active=True).order_by('first_name'),
        })


class ProjectLocationAddView(LoginRequiredMixin, View):
    def post(self, request, pk):
        project = get_object_or_404(Project, pk=pk)
        if not _is_project_member(request.user, project):
            return HttpResponseForbidden()
        try:
            lat = float(request.POST['latitude'])
            lng = float(request.POST['longitude'])
        except (KeyError, ValueError):
            messages.error(request, 'Valid latitude and longitude are required.')
            return redirect('project_locations', pk=pk)

        assigned = User.objects.filter(pk=request.POST.get('assigned_to')).first()
        ProjectLocation.objects.create(
            project=project,
            name=request.POST.get('name', '').strip(),
            description=request.POST.get('description', '').strip(),
            address=request.POST.get('address', '').strip(),
            latitude=lat,
            longitude=lng,
            category=request.POST.get('category', '').strip(),
            status=request.POST.get('status', 'pending'),
            assigned_to=assigned,
            notes=request.POST.get('notes', '').strip(),
        )
        messages.success(request, 'Location added.')
        return redirect('project_locations', pk=pk)


class ProjectLocationEditView(LoginRequiredMixin, View):
    def post(self, request, pk, lid):
        project = get_object_or_404(Project, pk=pk)
        if not _is_project_member(request.user, project):
            return HttpResponseForbidden()
        loc = get_object_or_404(ProjectLocation, pk=lid, project=project)
        try:
            lat = float(request.POST['latitude'])
            lng = float(request.POST['longitude'])
        except (KeyError, ValueError):
            messages.error(request, 'Valid coordinates required.')
            return redirect('project_locations', pk=pk)

        loc.name = request.POST.get('name', '').strip()
        loc.description = request.POST.get('description', '').strip()
        loc.address = request.POST.get('address', '').strip()
        loc.latitude = lat
        loc.longitude = lng
        loc.category = request.POST.get('category', '').strip()
        loc.status = request.POST.get('status', 'pending')
        loc.assigned_to = User.objects.filter(pk=request.POST.get('assigned_to')).first()
        loc.notes = request.POST.get('notes', '').strip()
        loc.save()
        messages.success(request, 'Location updated.')
        return redirect('project_locations', pk=pk)


class ProjectLocationDeleteView(LoginRequiredMixin, View):
    def post(self, request, pk, lid):
        project = get_object_or_404(Project, pk=pk)
        if not _is_project_member(request.user, project):
            return HttpResponseForbidden()
        get_object_or_404(ProjectLocation, pk=lid, project=project).delete()
        messages.success(request, 'Location removed.')
        return redirect('project_locations', pk=pk)


class LocationMapHTMLExportView(LoginRequiredMixin, View):
    """Download a self-contained interactive Leaflet map as an HTML file."""

    _STATUS_COLORS = {
        'pending': '#94a3b8',
        'in_progress': '#3b82f6',
        'completed': '#10b981',
        'on_hold': '#f59e0b',
    }

    def get(self, request, pk):
        project = get_object_or_404(Project, pk=pk)
        locations = project.locations.select_related('assigned_to').order_by('name')
        status_labels = dict(ProjectLocation.Status.choices)

        loc_data = [
            {
                'name': loc.name,
                'description': loc.description,
                'address': loc.address,
                'lat': float(loc.latitude),
                'lng': float(loc.longitude),
                'category': loc.category,
                'status': loc.status,
                'statusLabel': status_labels.get(loc.status, loc.status),
                'color': self._STATUS_COLORS.get(loc.status, '#64748b'),
                'assignedTo': loc.assigned_to.get_full_name() if loc.assigned_to else '',
                'notes': loc.notes,
            }
            for loc in locations
        ]

        html = self._render_html(project, loc_data)
        resp = HttpResponse(html, content_type='text/html; charset=utf-8')
        resp['Content-Disposition'] = f'attachment; filename="{project.code}_location_map.html"'
        return resp

    def _render_html(self, project, loc_data):
        import json as _json
        count = len(loc_data)
        from django.utils.timezone import now
        exported = now().strftime('%d %b %Y')
        colors = self._STATUS_COLORS

        return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>{project.name} — Location Map</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" crossorigin=""/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js" crossorigin=""></script>
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{font-family:system-ui,-apple-system,sans-serif;background:#f8fafc;display:flex;flex-direction:column;height:100vh}}
  header{{background:{project.cover_color};color:#fff;padding:14px 22px;display:flex;align-items:center;justify-content:space-between;flex-shrink:0}}
  header h1{{font-size:1.1rem;font-weight:800;margin-bottom:2px}}
  header p{{font-size:.75rem;opacity:.75}}
  #map{{flex:1}}
  .legend{{background:#fff;padding:12px 16px;border-radius:10px;box-shadow:0 2px 12px rgba(0,0,0,.15);font-size:.78rem;line-height:1.8}}
  .legend b{{display:block;margin-bottom:6px;font-size:.75rem;letter-spacing:.05em;color:#64748b}}
  .legend-item{{display:flex;align-items:center;gap:8px}}
  .ldot{{width:11px;height:11px;border-radius:50%;flex-shrink:0}}
  .popup-name{{font-weight:700;font-size:.92rem;margin-bottom:5px}}
  .popup-badge{{display:inline-block;padding:2px 8px;border-radius:5px;font-size:.7rem;font-weight:600;margin-bottom:7px}}
  .popup-row{{font-size:.8rem;color:#475569;margin-bottom:3px;display:flex;gap:5px}}
  .popup-row b{{color:#1e293b;flex-shrink:0}}
  .popup-coords{{font-size:.7rem;color:#94a3b8;margin-top:7px;font-family:monospace}}
  .sidebar{{position:absolute;top:10px;left:10px;z-index:900;max-height:calc(100% - 20px);overflow-y:auto;background:#fff;border-radius:10px;box-shadow:0 2px 12px rgba(0,0,0,.15);width:240px}}
  .sidebar-header{{padding:12px 14px 8px;border-bottom:1px solid #e2e8f0;font-size:.8rem;font-weight:700;color:#1e293b}}
  .sidebar-item{{padding:9px 14px;border-bottom:1px solid #f1f5f9;cursor:pointer;transition:background .1s}}
  .sidebar-item:hover{{background:#f8fafc}}
  .sidebar-name{{font-size:.82rem;font-weight:600;color:#1e293b}}
  .sidebar-sub{{font-size:.72rem;color:#94a3b8;margin-top:1px}}
  .sidebar-dot{{width:9px;height:9px;border-radius:50%;flex-shrink:0;margin-top:4px}}
</style>
</head>
<body>
<header>
  <div>
    <h1>{project.name}</h1>
    <p>{project.code} · {count} location(s) · Exported {exported}</p>
  </div>
  <div style="font-size:.75rem;opacity:.6">Interactive — click pins for details</div>
</header>
<div style="position:relative;flex:1;display:flex">
  <div id="map"></div>
  <div class="sidebar" id="sidebar">
    <div class="sidebar-header">📍 Locations ({count})</div>
    <div id="sidebar-list"></div>
  </div>
</div>
<script>
var LOCS = {_json.dumps(loc_data)};
var DEFAULT_CENTER = [13.4549, -16.5790];
var map = L.map('map');

L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png',{{
  attribution:'© <a href="https://www.openstreetmap.org">OpenStreetMap</a> contributors',
  maxZoom:19
}}).addTo(map);

function makeIcon(color){{
  var svg='<svg xmlns="http://www.w3.org/2000/svg" width="30" height="38" viewBox="0 0 30 38">'
    +'<path d="M15 0C7 0 0 7 0 15c0 10 15 23 15 23S30 25 30 15C30 7 23 0 15 0z" fill="'+color+'" stroke="#fff" stroke-width="1.5"/>'
    +'<circle cx="15" cy="15" r="6" fill="white"/>'
    +'</svg>';
  return L.divIcon({{html:svg,className:'',iconSize:[30,38],iconAnchor:[15,38],popupAnchor:[0,-40]}});
}}

var markers = [];
var bounds  = [];
var sidebarList = document.getElementById('sidebar-list');

LOCS.forEach(function(loc, i){{
  var m = L.marker([loc.lat, loc.lng], {{icon:makeIcon(loc.color)}}).addTo(map);
  var popup = '<div class="popup-name">'+loc.name+'</div>'
    + '<span class="popup-badge" style="background:'+loc.color+'20;color:'+loc.color+'">'+loc.statusLabel+'</span>';
  if(loc.category) popup+='<div class="popup-row"><b>Category:</b>'+loc.category+'</div>';
  if(loc.address)  popup+='<div class="popup-row"><b>Address:</b>'+loc.address+'</div>';
  if(loc.assignedTo) popup+='<div class="popup-row"><b>Assigned:</b>'+loc.assignedTo+'</div>';
  if(loc.description) popup+='<div class="popup-row">'+loc.description+'</div>';
  if(loc.notes) popup+='<div class="popup-row" style="font-style:italic;color:#64748b">'+loc.notes+'</div>';
  popup+='<div class="popup-coords">'+loc.lat.toFixed(5)+', '+loc.lng.toFixed(5)+'</div>';
  m.bindPopup(popup,{{maxWidth:270}});
  markers.push(m);
  bounds.push([loc.lat, loc.lng]);

  // Sidebar item
  var div = document.createElement('div');
  div.className='sidebar-item';
  div.innerHTML='<div style="display:flex;gap:8px"><div class="sidebar-dot" style="background:'+loc.color+'"></div>'
    +'<div><div class="sidebar-name">'+loc.name+'</div>'
    +'<div class="sidebar-sub">'+loc.statusLabel+(loc.category?' · '+loc.category:'')+'</div></div></div>';
  div.onclick=function(){{map.setView([loc.lat,loc.lng],16);m.openPopup();}};
  sidebarList.appendChild(div);
}});

if(bounds.length>1) map.fitBounds(bounds,{{padding:[40,40]}});
else if(bounds.length===1) map.setView(bounds[0],15);
else map.setView(DEFAULT_CENTER,8);

// Legend
var legend=L.control({{position:'bottomright'}});
legend.onAdd=function(){{
  var d=L.DomUtil.create('div','legend');
  d.innerHTML='<b>STATUS</b>';
  [{{'color':'{colors["pending"]}','label':'Pending'}},{{'color':'{colors["in_progress"]}','label':'In Progress'}},{{'color':'{colors["completed"]}','label':'Completed'}},{{'color':'{colors["on_hold"]}','label':'On Hold'}}]
    .forEach(function(s){{
      d.innerHTML+='<div class="legend-item"><div class="ldot" style="background:'+s.color+'"></div>'+s.label+'</div>';
    }});
  return d;
}};
legend.addTo(map);
</script>
</body>
</html>"""


class LocationMapPDFExportView(LoginRequiredMixin, View):
    """Export a formatted PDF table of all project locations."""

    def get(self, request, pk):
        project = get_object_or_404(Project, pk=pk)
        locations = list(project.locations.select_related('assigned_to').order_by('name'))

        try:
            from reportlab.lib.pagesizes import A4, landscape
            from reportlab.lib import colors as RC
            from reportlab.platypus import (
                SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
            )
            from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
            from reportlab.lib.units import mm
            import io as _io

            buf = _io.BytesIO()
            doc = SimpleDocTemplate(
                buf, pagesize=landscape(A4),
                topMargin=16 * mm, bottomMargin=16 * mm,
                leftMargin=14 * mm, rightMargin=14 * mm,
            )

            hex_ = project.cover_color.lstrip('#')
            C_COVER = RC.Color(*[int(hex_[i:i + 2], 16) / 255 for i in (0, 2, 4)])

            S = getSampleStyleSheet()
            title_style = ParagraphStyle('T', fontSize=16, fontName='Helvetica-Bold',
                                         textColor=C_COVER, spaceAfter=3)
            sub_style = ParagraphStyle('S', fontSize=9, fontName='Helvetica',
                                       textColor=RC.HexColor('#64748b'), spaceAfter=14)
            note_style = ParagraphStyle('N', fontSize=8, fontName='Helvetica',
                                        textColor=RC.HexColor('#475569'), spaceAfter=5)

            STATUS_C = {
                'pending': RC.HexColor('#94a3b8'),
                'in_progress': RC.HexColor('#3b82f6'),
                'completed': RC.HexColor('#10b981'),
                'on_hold': RC.HexColor('#f59e0b'),
            }
            STATUS_L = dict(ProjectLocation.Status.choices)

            from django.utils.timezone import now
            story = [
                Paragraph(f'{project.name} — Location Map', title_style),
                Paragraph(
                    f'{project.code} · {len(locations)} location(s) · Exported {now().strftime("%d %b %Y")}',
                    sub_style
                ),
            ]

            # Table data
            headers = ['#', 'Location Name', 'Category', 'Address', 'Lat', 'Long', 'Status', 'Assigned To']
            rows = [headers]
            for i, loc in enumerate(locations, 1):
                rows.append([
                    str(i),
                    loc.name,
                    loc.category or '—',
                    loc.address or '—',
                    f'{float(loc.latitude):.5f}',
                    f'{float(loc.longitude):.5f}',
                    STATUS_L.get(loc.status, loc.status),
                    loc.assigned_to.get_full_name() if loc.assigned_to else '—',
                ])

            col_w = [8 * mm, 50 * mm, 32 * mm, 55 * mm, 22 * mm, 22 * mm, 24 * mm, 34 * mm]
            tbl = Table(rows, colWidths=col_w, repeatRows=1)
            style = TableStyle([
                ('BACKGROUND', (0, 0), (-1, 0), C_COVER),
                ('TEXTCOLOR', (0, 0), (-1, 0), RC.white),
                ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                ('FONTSIZE', (0, 0), (-1, 0), 8),
                ('FONTSIZE', (0, 1), (-1, -1), 7.5),
                ('ROWBACKGROUNDS', (0, 1), (-1, -1), [RC.white, RC.HexColor('#f8fafc')]),
                ('GRID', (0, 0), (-1, -1), 0.3, RC.HexColor('#e2e8f0')),
                ('ALIGN', (0, 0), (0, -1), 'CENTER'),
                ('ALIGN', (4, 1), (5, -1), 'CENTER'),
                ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
                ('TOPPADDING', (0, 0), (-1, -1), 4),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
                ('LEFTPADDING', (0, 0), (-1, -1), 4),
                ('RIGHTPADDING', (0, 0), (-1, -1), 4),
            ])
            # Colour status column per row
            for ri, loc in enumerate(locations, 1):
                sc = STATUS_C.get(loc.status, RC.HexColor('#64748b'))
                style.add('TEXTCOLOR', (6, ri), (6, ri), sc)
                style.add('FONTNAME', (6, ri), (6, ri), 'Helvetica-Bold')
            tbl.setStyle(style)
            story.append(tbl)

            # Notes
            locs_with_notes = [(l, l.notes) for l in locations if l.notes.strip()]
            if locs_with_notes:
                story += [Spacer(1, 14),
                          Paragraph('Notes', ParagraphStyle('H2', fontSize=10, fontName='Helvetica-Bold',
                                                            textColor=C_COVER, spaceAfter=6))]
                for loc, note in locs_with_notes:
                    story.append(Paragraph(f'<b>{loc.name}:</b> {note}', note_style))

            doc.build(story)
            buf.seek(0)
            return HttpResponse(
                buf.getvalue(),
                content_type='application/pdf',
                headers={'Content-Disposition': f'attachment; filename="{project.code}_locations.pdf"'},
            )

        except ImportError:
            return HttpResponse('ReportLab is required for PDF export.', status=500)