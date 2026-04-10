import io, re, json, tempfile, os
from datetime import date as _date, timedelta
from datetime import datetime

from django.contrib.auth.mixins import LoginRequiredMixin
from django.views.generic import TemplateView, View, DetailView
from django.shortcuts import render, redirect, get_object_or_404
from django.http import JsonResponse, HttpResponseForbidden, HttpResponse
from django.contrib import messages
from django.utils import timezone
from django.core.mail import send_mail
from django.conf import settings
from django.db.models import Q
from django.core.files.base import ContentFile

from apps.meetings.models import (
    Meeting, MeetingAttendee, MeetingExternalAttendee,
    MeetingMinutes, MeetingActionItem, MeetingMinutesExternalReview
)
from apps.core.models import User


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _meeting_is_locked(meeting):
    return meeting.status in ['completed', 'cancelled']

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
    try:
        from apps.core.models import CoreNotification
    except ImportError:
        CoreNotification = None

    exclude = set(exclude_ids or [])
    exclude.add(sender.id)

    start_text = timezone.localtime(meeting.start_datetime).strftime('%d %b %Y at %H:%M')
    end_text = timezone.localtime(meeting.end_datetime).strftime('%H:%M')
    location_text = meeting.location or 'Not specified'
    virtual_text = meeting.virtual_link or 'Not provided'
    agenda_text = (meeting.agenda or '').strip() or 'No agenda was provided.'
    project_text = meeting.project.name if meeting.project else 'Not linked to a project'

    email_body = (
        f'Hello,\n\n'
        f'You have been invited to the meeting "{meeting.title}".\n\n'
        f'Details:\n'
        f'- Organiser: {sender.full_name}\n'
        f'- Type: {meeting.get_meeting_type_display()}\n'
        f'- Date: {start_text}\n'
        f'- End Time: {end_text}\n'
        f'- Location: {location_text}\n'
        f'- Virtual Link: {virtual_text}\n'
        f'- Project: {project_text}\n\n'
        f'Description:\n'
        f'{meeting.description or "No description provided."}\n\n'
        f'Agenda:\n'
        f'{agenda_text}\n\n'
        f'Please check EasyOffice for more details.\n\n'
        f'— EasyOffice'
    )

    for att in meeting.attendees.exclude(user_id__in=exclude).select_related('user'):
        if CoreNotification:
            CoreNotification.objects.create(
                recipient=att.user,
                sender=sender,
                notification_type='meeting',
                title=title,
                message=message,
                link=f'/meetings/{meeting.id}/',
            )

        if att.user.email:
            try:
                send_mail(
                    subject=title,
                    message=email_body,
                    from_email=getattr(settings, 'DEFAULT_FROM_EMAIL', 'noreply@easyoffice.local'),
                    recipient_list=[att.user.email],
                    fail_silently=True,
                )
            except Exception:
                pass


def _notify_external_attendees(meeting, sender, title):
    start_text = timezone.localtime(meeting.start_datetime).strftime('%d %b %Y at %H:%M')
    end_text = timezone.localtime(meeting.end_datetime).strftime('%H:%M')
    location_text = meeting.location or 'Not specified'
    virtual_text = meeting.virtual_link or 'Not provided'
    agenda_text = (meeting.agenda or '').strip() or 'No agenda was provided.'
    project_text = meeting.project.name if meeting.project else 'Not linked to a project'

    for att in meeting.external_attendees.all():
        body = (
            f'Hello {att.full_name},\n\n'
            f'You have been invited to the meeting "{meeting.title}".\n\n'
            f'Details:\n'
            f'- Organiser: {sender.full_name}\n'
            f'- Type: {meeting.get_meeting_type_display()}\n'
            f'- Date: {start_text}\n'
            f'- End Time: {end_text}\n'
            f'- Location: {location_text}\n'
            f'- Virtual Link: {virtual_text}\n'
            f'- Project: {project_text}\n'
        )
        if att.organisation:
            body += f'- Organisation: {att.organisation}\n'
        if att.role:
            body += f'- Role: {att.role}\n'
        body += (
            f'\nDescription:\n'
            f'{meeting.description or "No description provided."}\n\n'
            f'Agenda:\n'
            f'{agenda_text}\n\n'
            f'Please keep this invitation for your reference.\n\n'
            f'— EasyOffice'
        )
        try:
            send_mail(
                subject=title,
                message=body,
                from_email=getattr(settings, 'DEFAULT_FROM_EMAIL', 'noreply@easyoffice.local'),
                recipient_list=[att.email],
                fail_silently=True,
            )
        except Exception:
            pass


def _notify_external_for_adoption(meeting, minutes, request):
    """
    Issue one-time review tokens to each external attendee and email them
    a personalised adopt/decline link.  Expires after 14 days.
    """
    from datetime import timedelta

    base_url = getattr(settings, 'SITE_URL', f'{request.scheme}://{request.get_host()}')
    expires  = timezone.now() + timedelta(days=14)

    start_text = timezone.localtime(meeting.start_datetime).strftime('%d %b %Y at %H:%M')

    for ext in meeting.external_attendees.all():
        # Create or refresh token
        review, created = MeetingMinutesExternalReview.objects.get_or_create(
            minutes=minutes,
            external_attendee=ext,
            defaults={'expires_at': expires},
        )
        if not created:
            # Refresh token and expiry if re-submitted
            from apps.meetings.models import _generate_review_token
            review.token      = _generate_review_token()
            review.decision   = MeetingMinutesExternalReview.Decision.PENDING
            review.decided_at = None
            review.expires_at = expires
            review.save()

        review_url = f'{base_url}/meetings/minutes/review/{review.token}/'

        body = (
            f'Dear {ext.full_name},\n\n'
            f'The minutes for the meeting "{meeting.title}" (held on {start_text}) '
            f'have been submitted for adoption.\n\n'
            f'As an external participant, you are invited to review and record your '
            f'position on these minutes.\n\n'
            f'Please use the link below — it is unique to you and valid for 14 days:\n\n'
            f'  {review_url}\n\n'
            f'You will be able to:\n'
            f'  • Adopt / Agree — confirm the minutes are accurate\n'
            f'  • Decline / Object — flag concerns or corrections\n'
            f'  • Add a comment with any notes or amendments\n\n'
            f'This link can only be used once.\n\n'
            f'— EasyOffice / {meeting.organizer.full_name}'
        )

        try:
            send_mail(
                subject=f'Action required: Review minutes for "{meeting.title}"',
                message=body,
                from_email=getattr(settings, 'DEFAULT_FROM_EMAIL', 'noreply@easyoffice.local'),
                recipient_list=[ext.email],
                fail_silently=True,
            )
        except Exception:
            pass


def _parse_meeting_datetime(value):
    if not value:
        raise ValueError('Date and time value is required.')
    value = str(value).strip()
    formats = [
        '%Y-%m-%dT%H:%M', '%Y-%m-%d %H:%M',
        '%Y-%m-%dT%H:%M:%S', '%Y-%m-%d %H:%M:%S',
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


def _next_occurrence(dt, recurrence):
    """Return the next datetime given a recurrence rule."""
    if recurrence == 'daily':
        return dt + timedelta(days=1)
    elif recurrence == 'weekly':
        return dt + timedelta(weeks=1)
    elif recurrence == 'biweekly':
        return dt + timedelta(weeks=2)
    elif recurrence == 'monthly':
        # Add ~30 days; same time of day
        from dateutil.relativedelta import relativedelta
        try:
            return dt + relativedelta(months=1)
        except Exception:
            return dt + timedelta(days=30)
    return None


def _generate_recurrence_instances(parent, attendee_ids, external_data):
    """Create child meeting instances for a recurring parent meeting."""
    recurrence = parent.recurrence
    if not recurrence or recurrence == 'none':
        return

    count = min(int(parent.recurrence_count or 4), 52)
    end_date = parent.recurrence_end  # may be None

    duration = parent.end_datetime - parent.start_datetime
    current_start = parent.start_datetime

    for _ in range(count):
        current_start = _next_occurrence(current_start, recurrence)
        if not current_start:
            break
        if end_date and current_start.date() > end_date:
            break

        instance = Meeting.objects.create(
            title=parent.title,
            description=parent.description,
            meeting_type=parent.meeting_type,
            organizer=parent.organizer,
            start_datetime=current_start,
            end_datetime=current_start + duration,
            location=parent.location,
            virtual_link=parent.virtual_link,
            agenda=parent.agenda,
            is_private=parent.is_private,
            project=parent.project,
            unit=parent.unit,
            department=parent.department,
            recurrence_parent=parent,
            recurrence='none',
        )

        # Copy attendees
        for uid in attendee_ids:
            try:
                u = User.objects.get(pk=uid)
                if u != parent.organizer:
                    MeetingAttendee.objects.get_or_create(
                        meeting=instance, user=u,
                        defaults={'is_required': True, 'rsvp': MeetingAttendee.RSVP.INVITED}
                    )
            except User.DoesNotExist:
                continue

        MeetingAttendee.objects.get_or_create(
            meeting=instance,
            user=parent.organizer,
            defaults={
                'rsvp': MeetingAttendee.RSVP.ACCEPTED,
                'is_required': True,
                'role': 'Organiser',
                'responded_at': timezone.now(),
            }
        )

        # Copy external attendees
        for ext in external_data:
            MeetingExternalAttendee.objects.get_or_create(
                meeting=instance,
                email=ext['email'],
                defaults={
                    'full_name': ext['full_name'],
                    'organisation': ext.get('organisation', ''),
                    'role': ext.get('role', ''),
                    'is_required': True,
                    'rsvp': MeetingExternalAttendee.RSVP.INVITED,
                }
            )


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
        buf, pagesize=A4, rightMargin=M, leftMargin=M,
        topMargin=28 * mm, bottomMargin=18 * mm,
        title=f'Minutes — {meeting.title}'
    )

    C_HEAD  = colors.HexColor('#1e3a5f')
    C_ACC   = colors.HexColor(meeting.type_color)
    C_MUTED = colors.HexColor('#64748b')
    C_BG    = colors.HexColor('#f8fafc')

    ss = getSampleStyleSheet()

    sTitle   = ParagraphStyle('MTitle',   parent=ss['Title'],   fontSize=20, textColor=C_HEAD, spaceAfter=3*mm, leading=26)
    sMeta    = ParagraphStyle('MMeta',    parent=ss['Normal'],  fontSize=9,  textColor=C_MUTED, spaceAfter=4*mm)
    sSection = ParagraphStyle('MSection', parent=ss['Heading2'],fontSize=13, textColor=C_HEAD, spaceBefore=6*mm, spaceAfter=3*mm)
    sBody    = ParagraphStyle('MBody',    parent=ss['Normal'],  fontSize=10.5, leading=16, spaceAfter=3*mm, alignment=TA_JUSTIFY)
    sBullet  = ParagraphStyle('MBullet',  parent=ss['Normal'],  fontSize=10.5, leading=15, spaceAfter=2*mm, leftIndent=10*mm, firstLineIndent=-5*mm)
    sAction  = ParagraphStyle('MAction',  parent=ss['Normal'],  fontSize=10, leading=14, spaceAfter=1*mm)

    def inline(text):
        text = text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
        text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
        text = re.sub(r'\*(.+?)\*',     r'<i>\1</i>', text)
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
                flush(); flowables.append(Paragraph(inline(s[2:]), sSection))
            elif s.startswith('## '):
                flush(); flowables.append(Paragraph(inline(s[3:]), sSection))
            elif s.startswith(('- ', '• ', '* ')):
                flush(); flowables.append(Paragraph(f'•  {inline(s[2:])}', sBullet))
            elif s == '':
                flush(); flowables.append(Spacer(1, 2*mm))
            else:
                buf_lines.append(s)
        flush()
        return flowables

    story = []

    story.append(Paragraph(meeting.title, sTitle))
    start = timezone.localtime(meeting.start_datetime).strftime('%A, %d %B %Y  %H:%M')
    end   = timezone.localtime(meeting.end_datetime).strftime('%H:%M')
    meta  = f'{start} – {end}  ·  {meeting.get_meeting_type_display()}  ·  Organised by {meeting.organizer.full_name}'
    if meeting.location:
        meta += f'  ·  {meeting.location}'

    # Adoption status in PDF
    if minutes.adoption_status == 'adopted':
        adopted_by = minutes.adopted_by.full_name if minutes.adopted_by else 'Unknown'
        adopted_at = timezone.localtime(minutes.adopted_at).strftime('%d %b %Y %H:%M') if minutes.adopted_at else '—'
        meta += f'\n\nAdopted by {adopted_by} on {adopted_at}'

    story.append(Paragraph(meta, sMeta))
    story.append(HRFlowable(width='100%', color=C_ACC, thickness=2, spaceAfter=5*mm))

    attendees = list(meeting.attendees.select_related('user').order_by('user__first_name'))
    if attendees:
        story.append(Paragraph('Attendees', sSection))
        rows = [[Paragraph('<b>Name</b>', sAction), Paragraph('<b>Role</b>', sAction), Paragraph('<b>Status</b>', sAction)]]
        for att in attendees:
            rows.append([
                Paragraph(att.user.full_name, sAction),
                Paragraph(att.role or '—', sAction),
                Paragraph(att.get_rsvp_display(), sAction),
            ])
        tbl = Table(rows, colWidths=[90*mm, 60*mm, 40*mm])
        tbl.setStyle(TableStyle([
            ('BACKGROUND', (0,0), (-1,0), C_BG),
            ('FONTNAME',   (0,0), (-1,0), 'Helvetica-Bold'),
            ('FONTSIZE',   (0,0), (-1,-1), 9.5),
            ('GRID',       (0,0), (-1,-1), 0.4, colors.HexColor('#e2e8f0')),
            ('ROWBACKGROUNDS', (0,1), (-1,-1), [colors.white, C_BG]),
            ('TOPPADDING', (0,0), (-1,-1), 4),
            ('BOTTOMPADDING', (0,0), (-1,-1), 4),
        ]))
        story.append(tbl)
        story.append(Spacer(1, 4*mm))

    if meeting.agenda:
        story.append(Paragraph('Agenda', sSection))
        story.extend(parse_body(meeting.agenda))

    if minutes.content:
        story.append(Paragraph('Meeting Notes', sSection))
        story.extend(parse_body(minutes.content))

    if minutes.decisions:
        story.append(Paragraph('Decisions Made', sSection))
        story.extend(parse_body(minutes.decisions))

    if minutes.adoption_notes:
        story.append(Paragraph('Adoption Notes / Amendments', sSection))
        story.extend(parse_body(minutes.adoption_notes))

    action_items = list(minutes.action_items.select_related('assigned_to').all())
    if action_items:
        story.append(Paragraph('Action Items', sSection))
        rows = [[
            Paragraph('<b>#</b>', sAction),
            Paragraph('<b>Action</b>', sAction),
            Paragraph('<b>Owner</b>', sAction),
            Paragraph('<b>Due</b>', sAction),
            Paragraph('<b>Status</b>', sAction),
        ]]
        for i, ai in enumerate(action_items, 1):
            rows.append([
                Paragraph(str(i), sAction),
                Paragraph(inline(ai.description), sAction),
                Paragraph(ai.assigned_to.full_name if ai.assigned_to else '—', sAction),
                Paragraph(ai.due_date.strftime('%d %b %Y') if ai.due_date else '—', sAction),
                Paragraph(ai.get_status_display(), sAction),
            ])
        tbl = Table(rows, colWidths=[10*mm, 82*mm, 45*mm, 25*mm, 28*mm])
        tbl.setStyle(TableStyle([
            ('BACKGROUND',    (0,0), (-1,0), C_BG),
            ('FONTNAME',      (0,0), (-1,0), 'Helvetica-Bold'),
            ('FONTSIZE',      (0,0), (-1,-1), 9.5),
            ('GRID',          (0,0), (-1,-1), 0.4, colors.HexColor('#e2e8f0')),
            ('ROWBACKGROUNDS',(0,1), (-1,-1), [colors.white, C_BG]),
            ('TOPPADDING',    (0,0), (-1,-1), 4),
            ('BOTTOMPADDING', (0,0), (-1,-1), 4),
        ]))
        story.append(tbl)

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont('Helvetica', 8)
        canvas.setFillColor(C_MUTED)
        canvas.drawString(M, 12*mm, f'EasyOffice Meeting Minutes  ·  {meeting.title}')
        canvas.drawRightString(
            PAGE_W - M, 12*mm,
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
            Q(organizer=user) | Q(attendees__user=user) | Q(is_private=False)
        ).distinct().select_related('organizer', 'project')

        if q:
            base_qs = base_qs.filter(Q(title__icontains=q) | Q(description__icontains=q))
        if mtype:
            base_qs = base_qs.filter(meeting_type=mtype)

        upcoming = base_qs.filter(
            start_datetime__gte=now, status__in=['scheduled', 'in_progress']
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
            'view': view,
            'q': q,
            'type_filter': mtype,
            'meeting_types': Meeting.MeetingType.choices,
            'my_upcoming_count': my_meetings.filter(start_datetime__gte=now, status='scheduled').count(),
            'pending_rsvp': MeetingAttendee.objects.filter(user=user, rsvp='invited').count(),
        })
        return ctx


# ─────────────────────────────────────────────────────────────────────────────
# Create / Edit helpers
# ─────────────────────────────────────────────────────────────────────────────

def _meeting_form_context(request, meeting=None, prefill=None):
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
        'recurrence_types': Meeting.Recurrence.choices,
        'meeting': meeting or prefill,
        'mode': 'edit' if meeting else 'create',
        'selected_project_id': selected_project_id,
        'external_attendees': meeting.external_attendees.all() if meeting else [],
        'prefill': prefill,
    }

    if meeting:
        ctx['current_attendee_ids'] = list(meeting.attendees.values_list('user_id', flat=True))
        ctx['available_tasks'] = Task.objects.filter(
            Q(assigned_by=request.user) | Q(assigned_to=request.user)
        ).filter(status__in=['todo', 'in_progress', 'review']).order_by('-created_at')[:50]
        ctx['linked_task_ids'] = list(meeting.linked_tasks.values_list('id', flat=True))
    elif prefill:
        # Follow-up: pre-select same attendees
        ctx['current_attendee_ids'] = list(prefill.attendees.values_list('user_id', flat=True))
        ctx['available_tasks'] = Task.objects.filter(
            Q(assigned_by=request.user) | Q(assigned_to=request.user)
        ).filter(status__in=['todo', 'in_progress', 'review']).order_by('-created_at')[:50]
        ctx['linked_task_ids'] = []
    else:
        from apps.tasks.models import Task as T
        ctx['available_tasks'] = T.objects.filter(
            Q(assigned_by=request.user) | Q(assigned_to=request.user)
        ).filter(status__in=['todo', 'in_progress', 'review']).order_by('-created_at')[:50]
        ctx['linked_task_ids'] = []
        ctx['current_attendee_ids'] = []

    return ctx


def _save_attendees_and_external(meeting, request, is_update=False):
    """Shared logic to persist internal + external attendees from POST."""
    attendee_ids = set(request.POST.getlist('attendees'))

    if is_update:
        existing_ids = set(
            str(i) for i in meeting.attendees.exclude(user=request.user).values_list('user_id', flat=True)
        )
        removed_ids = existing_ids - attendee_ids
        if removed_ids:
            meeting.attendees.filter(user_id__in=removed_ids).delete()
        new_ids = attendee_ids - existing_ids
    else:
        new_ids = attendee_ids

    for uid in new_ids:
        try:
            u = User.objects.get(pk=uid)
            if u != request.user:
                MeetingAttendee.objects.get_or_create(
                    meeting=meeting, user=u,
                    defaults={'is_required': True, 'rsvp': MeetingAttendee.RSVP.INVITED}
                )
        except User.DoesNotExist:
            continue

    MeetingAttendee.objects.get_or_create(
        meeting=meeting, user=request.user,
        defaults={
            'rsvp': MeetingAttendee.RSVP.ACCEPTED,
            'is_required': True,
            'role': 'Organiser',
            'responded_at': timezone.now(),
        }
    )

    if is_update:
        meeting.external_attendees.all().delete()

    ext_names  = request.POST.getlist('external_name')
    ext_emails = request.POST.getlist('external_email')
    ext_orgs   = request.POST.getlist('external_organisation')
    ext_roles  = request.POST.getlist('external_role')

    external_data = []
    for i, raw_email in enumerate(ext_emails):
        email     = (raw_email or '').strip()
        full_name = (ext_names[i] if i < len(ext_names) else '').strip()
        org       = (ext_orgs[i]  if i < len(ext_orgs)  else '').strip()
        role      = (ext_roles[i] if i < len(ext_roles) else '').strip()
        if not email or not full_name:
            continue
        MeetingExternalAttendee.objects.get_or_create(
            meeting=meeting, email=email,
            defaults={'full_name': full_name, 'organisation': org, 'role': role,
                      'is_required': True, 'rsvp': MeetingExternalAttendee.RSVP.INVITED}
        )
        external_data.append({'email': email, 'full_name': full_name, 'organisation': org, 'role': role})

    return list(attendee_ids), external_data


# ─────────────────────────────────────────────────────────────────────────────
# Create
# ─────────────────────────────────────────────────────────────────────────────

class MeetingCreateView(LoginRequiredMixin, View):
    template_name = 'meetings/meeting_form.html'

    def get(self, request):
        prefill = None
        follow_up_from = request.GET.get('follow_up_from')
        if follow_up_from:
            try:
                prefill = Meeting.objects.get(pk=follow_up_from)
            except (Meeting.DoesNotExist, Exception):
                pass
        return render(request, self.template_name, _meeting_form_context(request, prefill=prefill))

    def post(self, request):
        start_raw = request.POST.get('start_datetime', '').strip()
        end_raw   = request.POST.get('end_datetime', '').strip()

        if not start_raw or not end_raw:
            messages.error(request, 'Start and end date/time are required.')
            return render(request, self.template_name, _meeting_form_context(request))

        try:
            start_dt = _parse_meeting_datetime(start_raw)
            end_dt   = _parse_meeting_datetime(end_raw)
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

        recurrence       = request.POST.get('recurrence', 'none')
        recurrence_count_raw = request.POST.get('recurrence_count', '').strip()
        recurrence_end_raw   = request.POST.get('recurrence_end', '').strip()

        recurrence_count = None
        recurrence_end   = None
        if recurrence != 'none':
            try:
                recurrence_count = max(1, min(52, int(recurrence_count_raw or 4)))
            except ValueError:
                recurrence_count = 4
            if recurrence_end_raw:
                try:
                    recurrence_end = datetime.strptime(recurrence_end_raw, '%Y-%m-%d').date()
                except ValueError:
                    recurrence_end = None

        parent_meeting_id = request.POST.get('parent_meeting') or None

        meeting = Meeting.objects.create(
            title=title,
            description=request.POST.get('description', '').strip(),
            meeting_type=request.POST.get('meeting_type', 'team'),
            organizer=request.user,
            start_datetime=start_dt,
            end_datetime=end_dt,
            location=request.POST.get('location', '').strip(),
            virtual_link=request.POST.get('virtual_link', '').strip(),
            agenda=request.POST.get('agenda', '').strip(),
            is_private='is_private' in request.POST,
            project_id=request.POST.get('project') or None,
            unit_id=request.POST.get('unit') or None,
            department_id=request.POST.get('department') or None,
            recurrence=recurrence,
            recurrence_count=recurrence_count,
            recurrence_end=recurrence_end,
            parent_meeting_id=parent_meeting_id,
        )

        attendee_ids, external_data = _save_attendees_and_external(meeting, request)

        task_ids = request.POST.getlist('linked_tasks')
        if task_ids:
            from apps.tasks.models import Task
            meeting.linked_tasks.set(Task.objects.filter(pk__in=task_ids))

        start_text = timezone.localtime(meeting.start_datetime).strftime('%d %b %Y at %H:%M')

        _notify_attendees(
            meeting, request.user,
            f'Meeting invitation: {meeting.title}',
            f'{request.user.full_name} invited you to "{meeting.title}" on {start_text}.',
        )
        try:
            _notify_external_attendees(meeting, request.user, f'Meeting invitation: {meeting.title}')
        except Exception:
            pass

        # Generate recurring instances
        if recurrence != 'none':
            try:
                _generate_recurrence_instances(meeting, attendee_ids, external_data)
                instance_count = min(recurrence_count or 4, 52)
                messages.success(request, f'Meeting "{meeting.title}" created with {instance_count} recurring instance(s).')
            except Exception as e:
                messages.warning(request, f'Meeting created, but recurrence generation failed: {e}')
        else:
            messages.success(request, f'Meeting "{meeting.title}" created.')

        return redirect('meeting_detail', pk=meeting.pk)


# ─────────────────────────────────────────────────────────────────────────────
# Update
# ─────────────────────────────────────────────────────────────────────────────

class MeetingUpdateView(LoginRequiredMixin, View):
    template_name = 'meetings/meeting_form.html'

    def get(self, request, pk):
        meeting = get_object_or_404(Meeting, pk=pk)
        if not _can_edit_meeting(request.user, meeting):
            return HttpResponseForbidden('Only the organiser can edit this meeting.')
        if _meeting_is_locked(meeting):
            messages.error(request, 'This meeting is closed and can no longer be edited.')
            return redirect('meeting_detail', pk=pk)
        return render(request, self.template_name, _meeting_form_context(request, meeting))

    def post(self, request, pk):
        meeting = get_object_or_404(Meeting, pk=pk)
        if not _can_edit_meeting(request.user, meeting):
            return HttpResponseForbidden('Only the organiser can edit this meeting.')
        if _meeting_is_locked(meeting):
            messages.error(request, 'This meeting is closed and can no longer be edited.')
            return redirect('meeting_detail', pk=pk)

        start_raw = request.POST.get('start_datetime', '').strip()
        end_raw   = request.POST.get('end_datetime', '').strip()

        if not start_raw or not end_raw:
            messages.error(request, 'Start and end date/time are required.')
            return render(request, self.template_name, _meeting_form_context(request, meeting))

        try:
            start_dt = _parse_meeting_datetime(start_raw)
            end_dt   = _parse_meeting_datetime(end_raw)
        except ValueError as e:
            messages.error(request, str(e))
            return render(request, self.template_name, _meeting_form_context(request, meeting))

        if end_dt <= start_dt:
            messages.error(request, 'End date/time must be after the start date/time.')
            return render(request, self.template_name, _meeting_form_context(request, meeting))

        title = request.POST.get('title', '').strip()
        if not title:
            messages.error(request, 'Meeting title is required.')
            return render(request, self.template_name, _meeting_form_context(request, meeting))

        meeting.title       = title
        meeting.description = request.POST.get('description', '').strip()
        meeting.meeting_type = request.POST.get('meeting_type', meeting.meeting_type)
        meeting.start_datetime = start_dt
        meeting.end_datetime   = end_dt
        meeting.location    = request.POST.get('location', '').strip()
        meeting.virtual_link = request.POST.get('virtual_link', '').strip()
        meeting.agenda      = request.POST.get('agenda', '').strip()
        meeting.is_private  = 'is_private' in request.POST
        meeting.project_id  = request.POST.get('project') or None
        meeting.unit_id     = request.POST.get('unit') or None
        meeting.department_id = request.POST.get('department') or None
        meeting.status      = request.POST.get('status', meeting.status)
        meeting.save()

        _save_attendees_and_external(meeting, request, is_update=True)

        task_ids = request.POST.getlist('linked_tasks')
        from apps.tasks.models import Task
        if task_ids:
            meeting.linked_tasks.set(Task.objects.filter(pk__in=task_ids))
        else:
            meeting.linked_tasks.clear()

        start_text = timezone.localtime(meeting.start_datetime).strftime('%d %b %Y at %H:%M')
        _notify_attendees(
            meeting, request.user,
            f'Meeting updated: {meeting.title}',
            f'The meeting "{meeting.title}" has been updated. It is scheduled for {start_text}.',
        )
        try:
            _notify_external_attendees(meeting, request.user, f'Meeting updated: {meeting.title}')
        except Exception:
            pass

        messages.success(request, 'Meeting updated.')
        return redirect('meeting_detail', pk=meeting.pk)


# ─────────────────────────────────────────────────────────────────────────────
# Follow-up
# ─────────────────────────────────────────────────────────────────────────────

class MeetingFollowUpView(LoginRequiredMixin, View):
    """Redirect to the create form pre-populated from a source meeting."""
    def get(self, request, pk):
        get_object_or_404(Meeting, pk=pk)  # 404 guard
        return redirect(f'/meetings/create/?follow_up_from={pk}')


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
        minutes   = getattr(meeting, 'minutes', None)

        ctx = {
            'meeting': meeting,
            'attendees': meeting.attendees.select_related('user').all(),
            'external_attendees': meeting.external_attendees.all(),
            'my_invite': my_invite,
            'minutes': minutes,
            'action_items': minutes.action_items.select_related('assigned_to').all() if minutes else [],
            'can_edit': _can_edit_meeting(request.user, meeting) and not _meeting_is_locked(meeting),
            'can_take_minutes': (
                request.user.is_superuser or
                meeting.organizer_id == request.user.id or
                meeting.attendees.filter(user=request.user).exists()
            ),
            'attendance_open': not _meeting_is_locked(meeting),
            'meeting_locked': _meeting_is_locked(meeting),
            'linked_tasks': meeting.linked_tasks.select_related('assigned_to').all(),
            'rsvp_choices': MeetingAttendee.RSVP.choices,
            # Recurrence context
            'recurrence_instances': meeting.recurrence_instances.order_by('start_datetime')[:12]
                                    if meeting.is_recurring_parent else [],
            'recurrence_parent': meeting.recurrence_parent,
            # Follow-up context
            'follow_up_meetings': meeting.follow_up_meetings.order_by('start_datetime')[:5],
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

        attendee, _ = MeetingAttendee.objects.get_or_create(meeting=meeting, user=request.user)
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
# Cancel / End
# ─────────────────────────────────────────────────────────────────────────────

class MeetingCancelView(LoginRequiredMixin, View):
    def post(self, request, pk):
        meeting = get_object_or_404(Meeting, pk=pk)
        if not _can_edit_meeting(request.user, meeting):
            return HttpResponseForbidden('Only the organiser can cancel this meeting.')
        meeting.status = 'cancelled'
        meeting.save(update_fields=['status'])
        _notify_attendees(meeting, request.user,
                          f'Meeting cancelled: {meeting.title}',
                          f'{request.user.full_name} has cancelled "{meeting.title}".')
        messages.warning(request, f'Meeting "{meeting.title}" cancelled.')
        return redirect('meeting_list')


class MeetingEndView(LoginRequiredMixin, View):
    def post(self, request, pk):
        meeting = get_object_or_404(Meeting, pk=pk)
        if not _can_edit_meeting(request.user, meeting):
            return HttpResponseForbidden('Only the organiser can end this meeting.')
        if meeting.status == 'cancelled':
            messages.error(request, 'A cancelled meeting cannot be ended.')
            return redirect('meeting_detail', pk=pk)
        if meeting.status == 'completed':
            messages.info(request, 'This meeting has already been ended.')
            return redirect('meeting_detail', pk=pk)

        meeting.status = 'completed'
        meeting.save(update_fields=['status'])

        _notify_attendees(meeting, request.user,
                          f'Meeting ended: {meeting.title}',
                          f'{request.user.full_name} has ended the meeting "{meeting.title}".')
        try:
            _notify_external_attendees(meeting, request.user, f'Meeting ended: {meeting.title}')
        except Exception:
            pass

        messages.success(request, f'Meeting "{meeting.title}" has been ended.')
        return redirect('meeting_detail', pk=pk)


# ─────────────────────────────────────────────────────────────────────────────
# Attendance
# ─────────────────────────────────────────────────────────────────────────────

class MeetingAttendanceUpdateView(LoginRequiredMixin, View):
    def post(self, request, pk):
        meeting = get_object_or_404(Meeting, pk=pk)
        if not _can_edit_meeting(request.user, meeting):
            return HttpResponseForbidden('Only the organiser can manage attendance.')
        if _meeting_is_locked(meeting):
            messages.error(request, 'Attendance is closed.')
            return redirect('meeting_detail', pk=pk)

        attendee_type = request.POST.get('attendee_type', 'internal')
        attendee_id   = request.POST.get('attendee_id')
        status        = request.POST.get('status')

        allowed = {'attended', 'no_show', 'accepted', 'declined', 'tentative'}
        if status not in allowed:
            messages.error(request, 'Invalid attendance status.')
            return redirect('meeting_detail', pk=pk)

        if attendee_type == 'external':
            attendee = get_object_or_404(MeetingExternalAttendee, pk=attendee_id, meeting=meeting)
        else:
            attendee = get_object_or_404(MeetingAttendee, pk=attendee_id, meeting=meeting)

        attendee.rsvp = status
        if status == 'attended':
            attendee.checked_in_at = timezone.now()
        attendee.responded_at = timezone.now()
        attendee.save(update_fields=['rsvp', 'checked_in_at', 'responded_at'])

        messages.success(request, 'Attendance updated.')
        return redirect('meeting_detail', pk=pk)


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
            meeting=meeting, defaults={'author': request.user}
        )
        staff = User.objects.filter(is_active=True).order_by('first_name')
        external_reviews = minutes.external_reviews.select_related('external_attendee').all() \
                           if minutes.adoption_status != 'draft' else []
        return render(request, self.template_name, {
            'meeting': meeting,
            'minutes': minutes,
            'action_items': minutes.action_items.select_related('assigned_to').all(),
            'staff_list': staff,
            'can_edit': minutes.is_editable or request.user.is_superuser,
            'action_statuses': MeetingActionItem.ActionStatus.choices,
            'external_reviews': external_reviews,
        })

    def post(self, request, pk):
        meeting = get_object_or_404(Meeting, pk=pk)
        if not _can_view_meeting(request.user, meeting):
            return HttpResponseForbidden()

        minutes, _ = MeetingMinutes.objects.get_or_create(
            meeting=meeting, defaults={'author': request.user}
        )

        action = request.POST.get('action', 'save')

        # ── SAVE / SUBMIT / ADOPT ──────────────────────────────────────────
        if action in ('save', 'submit_for_adoption') and (minutes.is_editable or request.user.is_superuser):
            minutes.content   = request.POST.get('content', '')
            minutes.decisions = request.POST.get('decisions', '')
            minutes.author    = request.user

            # Rebuild action items
            minutes.action_items.all().delete()
            descs    = request.POST.getlist('ai_description')
            owners   = request.POST.getlist('ai_owner')
            dues     = request.POST.getlist('ai_due')
            statuses = request.POST.getlist('ai_status')

            for i, desc in enumerate(descs):
                desc = desc.strip()
                if not desc:
                    continue
                ai_status = statuses[i] if i < len(statuses) and statuses[i] else 'not_started'
                if ai_status not in dict(MeetingActionItem.ActionStatus.choices):
                    ai_status = 'not_started'
                MeetingActionItem.objects.create(
                    minutes=minutes,
                    description=desc,
                    assigned_to_id=owners[i] if i < len(owners) and owners[i] else None,
                    due_date=dues[i] if i < len(dues) and dues[i] else None,
                    status=ai_status,
                )

            if action == 'submit_for_adoption':
                minutes.adoption_status = MeetingMinutes.AdoptionStatus.SUBMITTED
                minutes.is_final = True
                minutes.finalised_at = timezone.now()
                minutes.save()
                # Notify internal attendees
                _notify_attendees(
                    meeting, request.user,
                    f'Minutes submitted for adoption: {meeting.title}',
                    f'The minutes for "{meeting.title}" have been submitted for adoption. Please review.',
                )
                # Issue one-time review tokens to external attendees
                try:
                    _notify_external_for_adoption(meeting, minutes, request)
                except Exception as e:
                    messages.warning(request, f'External review emails failed: {e}')
                messages.success(request, 'Minutes submitted for adoption. Attendees have been notified to review.')
            else:
                minutes.save()
                messages.success(request, 'Draft minutes saved.')

        elif action == 'adopt' and not minutes.is_editable:
            # Organiser or superuser adopts the minutes
            if not (request.user.is_superuser or meeting.organizer_id == request.user.id):
                messages.error(request, 'Only the meeting organiser can adopt the minutes.')
                return redirect('meeting_minutes', pk=pk)

            minutes.adoption_status = MeetingMinutes.AdoptionStatus.ADOPTED
            minutes.adopted_at = timezone.now()
            minutes.adopted_by = request.user
            minutes.adoption_notes = request.POST.get('adoption_notes', '').strip()
            minutes.save()

            # Generate PDF
            try:
                pdf_bytes = _build_minutes_pdf(meeting, minutes)
                safe  = re.sub(r'[^\w\s-]', '', meeting.title)[:50].replace(' ', '-')
                fname = f'Minutes-{safe}-{timezone.localtime(meeting.start_datetime).strftime("%Y%m%d")}.pdf'
                _save_minutes_as_file(minutes, pdf_bytes, fname, request.user)
                messages.success(request, f'Minutes adopted and saved as "{fname}".')
            except Exception as e:
                messages.warning(request, f'Minutes adopted but PDF generation failed: {e}')

            _notify_attendees(
                meeting, request.user,
                f'Minutes adopted: {meeting.title}',
                f'The minutes for "{meeting.title}" have been officially adopted.',
            )

        elif action == 'reopen' and request.user.is_superuser:
            minutes.adoption_status = MeetingMinutes.AdoptionStatus.DRAFT
            minutes.is_final = False
            minutes.finalised_at = None
            minutes.save()
            messages.info(request, 'Minutes reopened for editing (superuser override).')

        return redirect('meeting_minutes', pk=pk)


def _save_minutes_as_file(minutes, pdf_bytes, filename, user):
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
    def get(self, request, pk):
        meeting = get_object_or_404(Meeting, pk=pk)
        if not _can_view_meeting(request.user, meeting):
            return HttpResponseForbidden()
        minutes = get_object_or_404(MeetingMinutes, meeting=meeting)
        pdf_bytes = _build_minutes_pdf(meeting, minutes)
        safe  = re.sub(r'[^\w\s-]', '', meeting.title)[:40].replace(' ', '-')
        fname = f'Minutes-{safe}.pdf'
        resp  = HttpResponse(pdf_bytes, content_type='application/pdf')
        resp['Content-Disposition'] = f'attachment; filename="{fname}"'
        return resp


class ShareMeetingMinutesView(LoginRequiredMixin, View):
    def post(self, request, pk):
        meeting = get_object_or_404(Meeting, pk=pk)
        if not _can_edit_meeting(request.user, meeting):
            return HttpResponseForbidden('Only the organiser can share minutes.')

        minutes = getattr(meeting, 'minutes', None)
        if not minutes:
            messages.error(request, 'No meeting minutes available yet.')
            return redirect('meeting_detail', pk=pk)

        _notify_attendees(meeting, request.user,
                          f'Meeting minutes shared: {meeting.title}',
                          f'The minutes for "{meeting.title}" are now available.')
        try:
            _notify_external_attendees(meeting, request.user, f'Meeting minutes shared: {meeting.title}')
        except Exception:
            pass

        messages.success(request, 'Meeting minutes shared with all attendees.')
        return redirect('meeting_detail', pk=pk)


# ─────────────────────────────────────────────────────────────────────────────
# Public: External attendee one-time review link
# ─────────────────────────────────────────────────────────────────────────────

class MeetingMinutesExternalReviewView(View):
    """
    Public (no login required) view served via a one-time token link
    sent to external attendees when minutes are submitted for adoption.
    GET  — show the minutes summary + adopt/decline form
    POST — record the decision and close the token
    """
    template_name = 'meetings/meeting_minutes_external_review.html'

    def _get_review(self, token):
        return get_object_or_404(MeetingMinutesExternalReview, token=token)

    def get(self, request, token):
        review  = self._get_review(token)
        meeting = review.minutes.meeting
        minutes = review.minutes
        return render(request, self.template_name, {
            'review':  review,
            'meeting': meeting,
            'minutes': minutes,
            'action_items': minutes.action_items.select_related('assigned_to').all(),
        })

    def post(self, request, token):
        review  = self._get_review(token)
        minutes = review.minutes
        meeting = minutes.meeting

        if review.is_expired:
            return render(request, self.template_name, {
                'review': review, 'meeting': meeting, 'minutes': minutes,
                'error': 'This review link has expired. Please contact the meeting organiser.',
            })

        if not review.is_pending:
            return render(request, self.template_name, {
                'review': review, 'meeting': meeting, 'minutes': minutes,
                'error': 'You have already submitted your response for these minutes.',
            })

        decision = request.POST.get('decision', '')
        if decision not in ('adopted', 'declined'):
            return render(request, self.template_name, {
                'review': review, 'meeting': meeting, 'minutes': minutes,
                'error': 'Please select either Adopt or Decline.',
            })

        review.decision   = decision
        review.comment    = request.POST.get('comment', '').strip()
        review.decided_at = timezone.now()
        review.save()

        # Notify the organiser in-app + email
        ext = review.external_attendee
        decision_label = review.get_decision_display()
        comment_text   = f'\n\nComment: {review.comment}' if review.comment else ''

        try:
            from apps.core.models import CoreNotification
            CoreNotification.objects.create(
                recipient=meeting.organizer,
                sender=None,
                notification_type='meeting',
                title=f'External review: {ext.full_name} {decision_label} the minutes',
                message=(
                    f'{ext.full_name} ({ext.email}) has {decision_label.lower()} '
                    f'the minutes for "{meeting.title}".{comment_text}'
                ),
                link=f'/meetings/{meeting.id}/minutes/',
            )
        except Exception:
            pass

        try:
            send_mail(
                subject=f'External review response: {meeting.title}',
                message=(
                    f'Hello {meeting.organizer.full_name},\n\n'
                    f'{ext.full_name} ({ext.email}) has submitted their review:\n\n'
                    f'Decision: {decision_label}\n'
                    f'Meeting: {meeting.title}\n'
                    f'Date: {timezone.localtime(meeting.start_datetime).strftime("%d %b %Y at %H:%M")}'
                    f'{comment_text}\n\n'
                    f'— EasyOffice'
                ),
                from_email=getattr(settings, 'DEFAULT_FROM_EMAIL', 'noreply@easyoffice.local'),
                recipient_list=[meeting.organizer.email],
                fail_silently=True,
            )
        except Exception:
            pass

        return render(request, self.template_name, {
            'review':    review,
            'meeting':   meeting,
            'minutes':   minutes,
            'submitted': True,
        })