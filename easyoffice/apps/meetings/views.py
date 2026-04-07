import io, re, json, tempfile, os
from datetime import date as _date
from datetime import datetime

from django.contrib.auth.mixins import LoginRequiredMixin
from django.views.generic import TemplateView, View, DetailView
from django.shortcuts import render, redirect, get_object_or_404
from django.http import JsonResponse, HttpResponseForbidden, HttpResponse
from django.contrib import messages
from django.utils import timezone
from django.db.models import Q
from django.core.files.base import ContentFile

from apps.meetings.models import (
    Meeting, MeetingAttendee, MeetingMinutes, MeetingActionItem
)
from apps.core.models import User


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _can_edit_meeting(user, meeting):
    return user.is_superuser or meeting.organizer_id == user.id


def _can_view_meeting(user, meeting):
    if user.is_superuser or meeting.organizer_id == user.id:
        return True
    if meeting.attendees.filter(user=user).exists():
        return True
    if not meeting.is_private:
        return True
    return False


def _notify_attendees(meeting, sender, title, message, exclude_ids=None):
    """Push CoreNotification to all attendees except sender."""
    try:
        from apps.core.models import CoreNotification
    except ImportError:
        return
    exclude = set(exclude_ids or [])
    exclude.add(sender.id)
    for att in meeting.attendees.exclude(user_id__in=exclude).select_related('user'):
        CoreNotification.objects.create(
            recipient=att.user,
            sender=sender,
            notification_type='meeting',
            title=title,
            message=message,
            link=f'/meetings/{meeting.id}/',
        )


def _parse_meeting_datetime(value):
    """
    Parse HTML datetime-local input (YYYY-MM-DDTHH:MM) or common datetime strings
    into a timezone-aware datetime.
    """
    if not value:
        raise ValueError('Date and time value is required.')

    value = str(value).strip()
    formats = [
        '%Y-%m-%dT%H:%M',
        '%Y-%m-%d %H:%M',
        '%Y-%m-%dT%H:%M:%S',
        '%Y-%m-%d %H:%M:%S',
    ]

    parsed = None
    for fmt in formats:
        try:
            parsed = datetime.strptime(value, fmt)
            break
        except ValueError:
            continue

    if parsed is None:
        raise ValueError('Invalid date/time format. Please use a valid date and time.')

    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, timezone.get_current_timezone())

    return parsed


# ─────────────────────────────────────────────────────────────────────────────
# Minutes PDF builder
# ─────────────────────────────────────────────────────────────────────────────

def _build_minutes_pdf(meeting, minutes):
    """Return raw PDF bytes for a meeting minutes document."""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_JUSTIFY
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer, HRFlowable, Table, TableStyle
    )

    buf = io.BytesIO()
    PAGE_W, PAGE_H = A4
    M = 20 * mm

    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        rightMargin=M,
        leftMargin=M,
        topMargin=28 * mm,
        bottomMargin=18 * mm,
        title=f'Minutes — {meeting.title}'
    )

    C_HEAD = colors.HexColor('#1e3a5f')
    C_ACC = colors.HexColor(meeting.type_color)
    C_MUTED = colors.HexColor('#64748b')
    C_BG = colors.HexColor('#f8fafc')

    ss = getSampleStyleSheet()

    sTitle = ParagraphStyle(
        'MTitle', parent=ss['Title'],
        fontSize=20, textColor=C_HEAD, spaceAfter=3 * mm, leading=26
    )
    sMeta = ParagraphStyle(
        'MMeta', parent=ss['Normal'],
        fontSize=9, textColor=C_MUTED, spaceAfter=4 * mm
    )
    sSection = ParagraphStyle(
        'MSection', parent=ss['Heading2'],
        fontSize=13, textColor=C_HEAD, spaceBefore=6 * mm, spaceAfter=3 * mm
    )
    sBody = ParagraphStyle(
        'MBody', parent=ss['Normal'],
        fontSize=10.5, leading=16, spaceAfter=3 * mm,
        alignment=TA_JUSTIFY
    )
    sBullet = ParagraphStyle(
        'MBullet', parent=ss['Normal'],
        fontSize=10.5, leading=15, spaceAfter=2 * mm,
        leftIndent=10 * mm, firstLineIndent=-5 * mm
    )
    sAction = ParagraphStyle(
        'MAction', parent=ss['Normal'],
        fontSize=10, leading=14, spaceAfter=1 * mm
    )

    def inline(text):
        text = text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
        text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
        text = re.sub(r'\*(.+?)\*', r'<i>\1</i>', text)
        return text

    def parse_body(text):
        flowables = []
        buf_lines = []

        def flush():
            if buf_lines:
                t = ' '.join(l for l in buf_lines if l)
                if t:
                    flowables.append(Paragraph(inline(t), sBody))
                buf_lines.clear()

        for line in text.splitlines():
            s = line.strip()
            if s.startswith('# '):
                flush()
                flowables.append(Paragraph(inline(s[2:]), sSection))
            elif s.startswith('## '):
                flush()
                flowables.append(Paragraph(inline(s[3:]), sSection))
            elif s.startswith(('- ', '• ', '* ')):
                flush()
                flowables.append(Paragraph(f'•  {inline(s[2:])}', sBullet))
            elif s == '':
                flush()
                flowables.append(Spacer(1, 2 * mm))
            else:
                buf_lines.append(s)
        flush()
        return flowables

    story = []

    story.append(Paragraph(meeting.title, sTitle))
    start = timezone.localtime(meeting.start_datetime).strftime('%A, %d %B %Y  %H:%M')
    end = timezone.localtime(meeting.end_datetime).strftime('%H:%M')
    meta = (
        f'{start} – {end}  ·  '
        f'{meeting.get_meeting_type_display()}  ·  '
        f'Organised by {meeting.organizer.full_name}'
    )
    if meeting.location:
        meta += f'  ·  {meeting.location}'
    story.append(Paragraph(meta, sMeta))
    story.append(HRFlowable(width='100%', color=C_ACC, thickness=2, spaceAfter=5 * mm))

    attendees = list(meeting.attendees.select_related('user').order_by('user__first_name'))
    if attendees:
        story.append(Paragraph('Attendees', sSection))
        rows = [[
            Paragraph('<b>Name</b>', sAction),
            Paragraph('<b>Role</b>', sAction),
            Paragraph('<b>Status</b>', sAction),
        ]]
        for att in attendees:
            rows.append([
                Paragraph(att.user.full_name, sAction),
                Paragraph(att.role or '—', sAction),
                Paragraph(att.get_rsvp_display(), sAction),
            ])
        tbl = Table(rows, colWidths=[90 * mm, 60 * mm, 40 * mm])
        tbl.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), C_BG),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, -1), 9.5),
            ('GRID', (0, 0), (-1, -1), 0.4, colors.HexColor('#e2e8f0')),
            ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, C_BG]),
            ('TOPPADDING', (0, 0), (-1, -1), 4),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
        ]))
        story.append(tbl)
        story.append(Spacer(1, 4 * mm))

    if meeting.agenda:
        story.append(Paragraph('Agenda', sSection))
        story.extend(parse_body(meeting.agenda))

    if minutes.content:
        story.append(Paragraph('Meeting Notes', sSection))
        story.extend(parse_body(minutes.content))

    if minutes.decisions:
        story.append(Paragraph('Decisions Made', sSection))
        story.extend(parse_body(minutes.decisions))

    action_items = list(minutes.action_items.select_related('assigned_to').all())
    if action_items:
        story.append(Paragraph('Action Items', sSection))
        rows = [[
            Paragraph('<b>#</b>', sAction),
            Paragraph('<b>Action</b>', sAction),
            Paragraph('<b>Owner</b>', sAction),
            Paragraph('<b>Due</b>', sAction),
            Paragraph('<b>Done</b>', sAction),
        ]]
        for i, ai in enumerate(action_items, 1):
            rows.append([
                Paragraph(str(i), sAction),
                Paragraph(inline(ai.description), sAction),
                Paragraph(ai.assigned_to.full_name if ai.assigned_to else '—', sAction),
                Paragraph(ai.due_date.strftime('%d %b %Y') if ai.due_date else '—', sAction),
                Paragraph('✓' if ai.is_done else '○', sAction),
            ])
        tbl = Table(rows, colWidths=[10 * mm, 85 * mm, 45 * mm, 25 * mm, 15 * mm])
        tbl.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), C_BG),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, -1), 9.5),
            ('GRID', (0, 0), (-1, -1), 0.4, colors.HexColor('#e2e8f0')),
            ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, C_BG]),
            ('TOPPADDING', (0, 0), (-1, -1), 4),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
        ]))
        story.append(tbl)

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont('Helvetica', 8)
        canvas.setFillColor(C_MUTED)
        canvas.drawString(M, 12 * mm, f'EasyOffice Meeting Minutes  ·  {meeting.title}')
        canvas.drawRightString(
            PAGE_W - M,
            12 * mm,
            f'Generated {timezone.now().strftime("%d %b %Y, %H:%M")}  ·  Page {doc.page}'
        )
        canvas.restoreState()

    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return buf.getvalue()


# ─────────────────────────────────────────────────────────────────────────────
# List
# ─────────────────────────────────────────────────────────────────────────────

class MeetingListView(LoginRequiredMixin, TemplateView):
    template_name = 'meetings/meeting_list.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        user = self.request.user
        now = timezone.now()
        view = self.request.GET.get('view', 'upcoming')
        q = self.request.GET.get('q', '')
        mtype = self.request.GET.get('type', '')

        base_qs = Meeting.objects.filter(
            Q(organizer=user) |
            Q(attendees__user=user) |
            Q(is_private=False)
        ).distinct().select_related('organizer', 'project')

        if q:
            base_qs = base_qs.filter(
                Q(title__icontains=q) | Q(description__icontains=q)
            )
        if mtype:
            base_qs = base_qs.filter(meeting_type=mtype)

        upcoming = base_qs.filter(
            start_datetime__gte=now,
            status__in=['scheduled', 'in_progress']
        ).order_by('start_datetime')

        past = base_qs.filter(
            Q(start_datetime__lt=now) | Q(status__in=['completed', 'cancelled'])
        ).order_by('-start_datetime')

        my_meetings = base_qs.filter(
            Q(organizer=user) | Q(attendees__user=user)
        ).distinct().order_by('start_datetime')

        ctx.update({
            'upcoming': upcoming[:20],
            'past': past[:20],
            'my_meetings': my_meetings.filter(start_datetime__gte=now)[:10],
            'view': view,
            'q': q,
            'type_filter': mtype,
            'meeting_types': Meeting.MeetingType.choices,
            'my_upcoming_count': my_meetings.filter(
                start_datetime__gte=now, status='scheduled'
            ).count(),
            'pending_rsvp': MeetingAttendee.objects.filter(
                user=user, rsvp='invited'
            ).count(),
        })
        return ctx


# ─────────────────────────────────────────────────────────────────────────────
# Create / Edit
# ─────────────────────────────────────────────────────────────────────────────

def _meeting_form_context(request, meeting=None):
    from apps.projects.models import Project
    from apps.tasks.models import Task
    try:
        from apps.organization.models import Unit, Department
        units = Unit.objects.all()
        depts = Department.objects.all()
    except Exception:
        units, depts = [], []

    selected_project_id = None
    if meeting and meeting.project_id:
        selected_project_id = str(meeting.project_id)
    else:
        selected_project_id = (request.GET.get('project') or request.POST.get('project') or '').strip()

    ctx = {
        'staff_list': User.objects.filter(is_active=True).order_by('first_name'),
        'projects': Project.objects.filter(status__in=['active', 'planning']),
        'units': units,
        'departments': depts,
        'meeting_types': Meeting.MeetingType.choices,
        'meeting': meeting,
        'mode': 'edit' if meeting else 'create',
        'selected_project_id': selected_project_id,
    }

    if meeting:
        ctx['current_attendee_ids'] = list(
            meeting.attendees.values_list('user_id', flat=True)
        )
        ctx['available_tasks'] = Task.objects.filter(
            Q(assigned_by=request.user) | Q(assigned_to=request.user)
        ).filter(status__in=['todo', 'in_progress', 'review']).order_by('-created_at')[:50]
        ctx['linked_task_ids'] = list(meeting.linked_tasks.values_list('id', flat=True))
    else:
        from apps.tasks.models import Task as T
        ctx['available_tasks'] = T.objects.filter(
            Q(assigned_by=request.user) | Q(assigned_to=request.user)
        ).filter(status__in=['todo', 'in_progress', 'review']).order_by('-created_at')[:50]
        ctx['linked_task_ids'] = []

    return ctx


class MeetingCreateView(LoginRequiredMixin, View):
    template_name = 'meetings/meeting_form.html'

    def get(self, request):
        return render(request, self.template_name, _meeting_form_context(request))

    def post(self, request):
        start_raw = request.POST.get('start_datetime', '').strip()
        end_raw = request.POST.get('end_datetime', '').strip()

        if not start_raw or not end_raw:
            messages.error(request, 'Start and end date/time are required.')
            return render(request, self.template_name, _meeting_form_context(request))

        try:
            start_dt = _parse_meeting_datetime(start_raw)
            end_dt = _parse_meeting_datetime(end_raw)
        except ValueError as e:
            messages.error(request, str(e))
            return render(request, self.template_name, _meeting_form_context(request))

        if end_dt <= start_dt:
            messages.error(request, 'End date/time must be after the start date/time.')
            return render(request, self.template_name, _meeting_form_context(request))

        title = request.POST.get('title', '').strip()
        if not title:
            messages.error(request, 'Meeting title is required.')
            return render(request, self.template_name, _meeting_form_context(request))

        meeting = Meeting.objects.create(
            title=title,
            description=request.POST.get('description', ''),
            meeting_type=request.POST.get('meeting_type', 'team'),
            organizer=request.user,
            start_datetime=start_dt,
            end_datetime=end_dt,
            location=request.POST.get('location', ''),
            virtual_link=request.POST.get('virtual_link', ''),
            agenda=request.POST.get('agenda', ''),
            is_private='is_private' in request.POST,
            project_id=request.POST.get('project') or None,
            unit_id=request.POST.get('unit') or None,
            department_id=request.POST.get('department') or None,
        )

        attendee_ids = request.POST.getlist('attendees')
        for uid in attendee_ids:
            try:
                u = User.objects.get(pk=uid)
                if u != request.user:
                    MeetingAttendee.objects.get_or_create(
                        meeting=meeting,
                        user=u,
                        defaults={'is_required': True}
                    )
            except User.DoesNotExist:
                pass

        MeetingAttendee.objects.get_or_create(
            meeting=meeting,
            user=request.user,
            defaults={'rsvp': 'accepted', 'is_required': True, 'role': 'Organiser'}
        )

        task_ids = request.POST.getlist('linked_tasks')
        if task_ids:
            from apps.tasks.models import Task
            meeting.linked_tasks.set(Task.objects.filter(pk__in=task_ids))

        start_text = timezone.localtime(meeting.start_datetime).strftime("%d %b %Y at %H:%M")
        _notify_attendees(
            meeting,
            request.user,
            f'Meeting invitation: {meeting.title}',
            f'{request.user.full_name} invited you to "{meeting.title}" on {start_text}.',
        )

        messages.success(request, f'Meeting "{meeting.title}" created.')
        return redirect('meeting_detail', pk=meeting.pk)


class MeetingUpdateView(LoginRequiredMixin, View):
    template_name = 'meetings/meeting_form.html'

    def get(self, request, pk):
        meeting = get_object_or_404(Meeting, pk=pk)
        if not _can_edit_meeting(request.user, meeting):
            return HttpResponseForbidden('Only the organiser can edit this meeting.')
        return render(request, self.template_name, _meeting_form_context(request, meeting))

    def post(self, request, pk):
        meeting = get_object_or_404(Meeting, pk=pk)
        if not _can_edit_meeting(request.user, meeting):
            return HttpResponseForbidden('Only the organiser can edit this meeting.')

        start_raw = request.POST.get('start_datetime', '').strip()
        end_raw = request.POST.get('end_datetime', '').strip()

        if not start_raw or not end_raw:
            messages.error(request, 'Start and end date/time are required.')
            return render(request, self.template_name, _meeting_form_context(request, meeting))

        try:
            start_dt = _parse_meeting_datetime(start_raw)
            end_dt = _parse_meeting_datetime(end_raw)
        except ValueError as e:
            messages.error(request, str(e))
            return render(request, self.template_name, _meeting_form_context(request, meeting))

        if end_dt <= start_dt:
            messages.error(request, 'End date/time must be after the start date/time.')
            return render(request, self.template_name, _meeting_form_context(request, meeting))

        meeting.title = request.POST.get('title', meeting.title).strip() or meeting.title
        meeting.description = request.POST.get('description', '')
        meeting.meeting_type = request.POST.get('meeting_type', meeting.meeting_type)
        meeting.start_datetime = start_dt
        meeting.end_datetime = end_dt
        meeting.location = request.POST.get('location', '')
        meeting.virtual_link = request.POST.get('virtual_link', '')
        meeting.agenda = request.POST.get('agenda', '')
        meeting.is_private = 'is_private' in request.POST
        meeting.project_id = request.POST.get('project') or None
        meeting.unit_id = request.POST.get('unit') or None
        meeting.department_id = request.POST.get('department') or None
        meeting.status = request.POST.get('status', meeting.status)
        meeting.save()

        attendee_ids = set(request.POST.getlist('attendees'))
        existing_ids = set(str(i) for i in meeting.attendees.exclude(
            user=request.user
        ).values_list('user_id', flat=True))

        removed = existing_ids - attendee_ids
        meeting.attendees.filter(user_id__in=removed).delete()

        new_ids = attendee_ids - existing_ids
        for uid in new_ids:
            try:
                u = User.objects.get(pk=uid)
                if u != request.user:
                    MeetingAttendee.objects.get_or_create(
                        meeting=meeting,
                        user=u,
                        defaults={'is_required': True}
                    )
            except User.DoesNotExist:
                pass

        task_ids = request.POST.getlist('linked_tasks')
        from apps.tasks.models import Task
        meeting.linked_tasks.set(Task.objects.filter(pk__in=task_ids) if task_ids else [])

        if new_ids:
            _notify_attendees(
                meeting,
                request.user,
                f'Meeting updated: {meeting.title}',
                f'The meeting "{meeting.title}" has been updated.',
            )

        messages.success(request, 'Meeting updated.')
        return redirect('meeting_detail', pk=meeting.pk)


# ─────────────────────────────────────────────────────────────────────────────
# Detail
# ─────────────────────────────────────────────────────────────────────────────

class MeetingDetailView(LoginRequiredMixin, View):
    template_name = 'meetings/meeting_detail.html'

    def get(self, request, pk):
        meeting = get_object_or_404(Meeting, pk=pk)
        if not _can_view_meeting(request.user, meeting):
            return HttpResponseForbidden('You do not have access to this meeting.')

        my_invite = meeting.attendees.filter(user=request.user).first()
        minutes = getattr(meeting, 'minutes', None)

        ctx = {
            'meeting': meeting,
            'attendees': meeting.attendees.select_related('user').all(),
            'my_invite': my_invite,
            'minutes': minutes,
            'action_items': minutes.action_items.select_related(
                'assigned_to', 'linked_task'
            ).all() if minutes else [],
            'can_edit': _can_edit_meeting(request.user, meeting),
            'can_take_minutes': (
                request.user.is_superuser or
                meeting.organizer_id == request.user.id or
                meeting.attendees.filter(user=request.user).exists()
            ),
            'linked_tasks': meeting.linked_tasks.select_related('assigned_to').all(),
            'rsvp_choices': MeetingAttendee.RSVP.choices,
        }
        return render(request, self.template_name, ctx)


# ─────────────────────────────────────────────────────────────────────────────
# RSVP
# ─────────────────────────────────────────────────────────────────────────────

class MeetingRSVPView(LoginRequiredMixin, View):
    def post(self, request, pk):
        meeting = get_object_or_404(Meeting, pk=pk)
        rsvp = request.POST.get('rsvp', 'accepted')
        if rsvp not in dict(MeetingAttendee.RSVP.choices):
            rsvp = 'accepted'

        attendee, _ = MeetingAttendee.objects.get_or_create(
            meeting=meeting, user=request.user
        )
        attendee.rsvp = rsvp
        attendee.responded_at = timezone.now()
        attendee.save(update_fields=['rsvp', 'responded_at'])

        try:
            from apps.core.models import CoreNotification
            CoreNotification.objects.create(
                recipient=meeting.organizer,
                sender=request.user,
                notification_type='meeting',
                title=f'{request.user.full_name} {rsvp} your meeting',
                message=f'{request.user.full_name} has {rsvp} the invitation to "{meeting.title}".',
                link=f'/meetings/{meeting.id}/',
            )
        except Exception:
            pass

        messages.success(request, f'RSVP recorded: {attendee.get_rsvp_display()}.')
        return redirect('meeting_detail', pk=pk)


# ─────────────────────────────────────────────────────────────────────────────
# Cancel
# ─────────────────────────────────────────────────────────────────────────────

class MeetingCancelView(LoginRequiredMixin, View):
    def post(self, request, pk):
        meeting = get_object_or_404(Meeting, pk=pk)
        if not _can_edit_meeting(request.user, meeting):
            return HttpResponseForbidden('Only the organiser can cancel this meeting.')

        meeting.status = 'cancelled'
        meeting.save(update_fields=['status'])

        _notify_attendees(
            meeting, request.user,
            f'Meeting cancelled: {meeting.title}',
            f'{request.user.full_name} has cancelled "{meeting.title}".',
        )
        messages.warning(request, f'Meeting "{meeting.title}" cancelled.')
        return redirect('meeting_list')


# ─────────────────────────────────────────────────────────────────────────────
# Minutes
# ─────────────────────────────────────────────────────────────────────────────

class MeetingMinutesView(LoginRequiredMixin, View):
    template_name = 'meetings/meeting_minutes.html'

    def get(self, request, pk):
        meeting = get_object_or_404(Meeting, pk=pk)
        if not _can_view_meeting(request.user, meeting):
            return HttpResponseForbidden()

        minutes, _ = MeetingMinutes.objects.get_or_create(
            meeting=meeting,
            defaults={'author': request.user}
        )
        staff = User.objects.filter(is_active=True).order_by('first_name')
        return render(request, self.template_name, {
            'meeting': meeting,
            'minutes': minutes,
            'action_items': minutes.action_items.select_related('assigned_to').all(),
            'staff_list': staff,
            'can_edit': not minutes.is_final or request.user.is_superuser,
        })

    def post(self, request, pk):
        meeting = get_object_or_404(Meeting, pk=pk)
        if not _can_view_meeting(request.user, meeting):
            return HttpResponseForbidden()

        minutes, _ = MeetingMinutes.objects.get_or_create(
            meeting=meeting,
            defaults={'author': request.user}
        )

        action = request.POST.get('action', 'save')

        if not minutes.is_final or request.user.is_superuser:
            minutes.content = request.POST.get('content', '')
            minutes.decisions = request.POST.get('decisions', '')
            minutes.author = request.user

            if action == 'finalise':
                minutes.is_final = True
                minutes.finalised_at = timezone.now()

            minutes.save()

            if not minutes.is_final or request.user.is_superuser:
                minutes.action_items.all().delete()
                descs = request.POST.getlist('ai_description')
                owners = request.POST.getlist('ai_owner')
                dues = request.POST.getlist('ai_due')
                dones = request.POST.getlist('ai_done')
                for i, desc in enumerate(descs):
                    desc = desc.strip()
                    if not desc:
                        continue
                    MeetingActionItem.objects.create(
                        minutes=minutes,
                        description=desc,
                        assigned_to_id=owners[i] if i < len(owners) and owners[i] else None,
                        due_date=dues[i] if i < len(dues) and dues[i] else None,
                        is_done=str(i) in dones,
                    )

            if action == 'finalise':
                try:
                    pdf_bytes = _build_minutes_pdf(meeting, minutes)
                    safe = re.sub(r'[^\w\s-]', '', meeting.title)[:50].replace(' ', '-')
                    fname = f'Minutes-{safe}-{timezone.localtime(meeting.start_datetime).strftime("%Y%m%d")}.pdf'
                    _save_minutes_as_file(minutes, pdf_bytes, fname, request.user)
                    messages.success(request, f'Minutes finalised and saved as "{fname}".')
                except Exception as e:
                    messages.warning(request, f'Minutes saved but PDF failed: {e}')
            else:
                messages.success(request, 'Draft minutes saved.')

            if action == 'finalise':
                _notify_attendees(
                    meeting, request.user,
                    f'Meeting minutes available: {meeting.title}',
                    f'The minutes for "{meeting.title}" have been finalised.',
                )

        return redirect('meeting_minutes', pk=pk)


def _save_minutes_as_file(minutes, pdf_bytes, filename, user):
    """Save the minutes PDF into the SharedFile system."""
    from apps.files.models import SharedFile
    sf = SharedFile.objects.create(
        name=filename,
        uploaded_by=user,
        visibility='unit',
        description=f'Meeting minutes: {minutes.meeting.title}',
        file_type='application/pdf',
        file_size=len(pdf_bytes),
    )
    sf.file.save(filename, ContentFile(pdf_bytes), save=True)
    try:
        sf.file_hash = sf.compute_hash()
        sf.save(update_fields=['file_hash'])
    except Exception:
        pass
    minutes.pdf_file = sf
    minutes.save(update_fields=['pdf_file'])
    return sf


class MeetingMinutesPDFView(LoginRequiredMixin, View):
    """Download the minutes PDF directly."""
    def get(self, request, pk):
        meeting = get_object_or_404(Meeting, pk=pk)
        if not _can_view_meeting(request.user, meeting):
            return HttpResponseForbidden()
        minutes = get_object_or_404(MeetingMinutes, meeting=meeting)
        pdf_bytes = _build_minutes_pdf(meeting, minutes)
        safe = re.sub(r'[^\w\s-]', '', meeting.title)[:40].replace(' ', '-')
        fname = f'Minutes-{safe}.pdf'
        resp = HttpResponse(pdf_bytes, content_type='application/pdf')
        resp['Content-Disposition'] = f'attachment; filename="{fname}"'
        return resp