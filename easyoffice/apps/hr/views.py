from calendar import monthrange
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from io import BytesIO

from django.conf import settings
from django.contrib import messages
from django.core.mail import EmailMessage
from django.core.exceptions import PermissionDenied
from django.contrib.auth import get_user_model
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db import transaction, IntegrityError
from django.db.models import Q, Sum
from django.http import HttpResponse, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.generic import DetailView, TemplateView, View

from apps.hr.models import (
    AppraisalKPI,
    AttendanceRecord,
    DisciplinaryAction,
    EmployeeDocument,
    EmployeeEmergencyContact,
    EmployeeEmployment,
    EmployeeOnboarding,
    EmployeeReference,
    EmployeeWorkHistory,
    HireCategory,
    HRSetting,
    PayrollRecord,
    PerformanceAppraisal,
    EmployeeOnboardingInvite,
)
from apps.staff.models import LeaveRequest
from apps.organization.models import Department, Unit, Position, OfficeLocation

from apps.hr.offer_letter import (
    build_offer_letter_pdf,
    offer_letter_filename,
    save_offer_letter_for_onboarding,
    send_offer_letter_for_signature,
)

from apps.hr.models import ZKDevice, ZKUserMap, ZKPunchLog
from apps.hr.zk_client import ZKClient, ZKConnectionError
from apps.hr import zk_service

User = get_user_model()


def money(value):
    return Decimal(value or 0).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


def parse_decimal(value, default='0'):
    try:
        return money(Decimal(str(value or default).replace(',', '')))
    except Exception:
        return money(default)


def parse_date(value):
    if not value:
        return None
    try:
        return timezone.datetime.strptime(value, '%Y-%m-%d').date()
    except (TypeError, ValueError):
        return None


def is_hr_user(user):
    return user.is_superuser or user.groups.filter(name='HR').exists()


def is_ceo_user(user):
    return user.is_superuser or user.groups.filter(name__in=['CEO', 'Chief Executive Officer']).exists()


def is_it_user(user):
    return user.is_superuser or user.groups.filter(name__in=['IT', 'ICT', 'Superuser of IT']).exists()


def is_supervisor_user(user):
    if not user.is_authenticated:
        return False
    if hasattr(user, 'staff_profile'):
        return getattr(user.staff_profile, 'is_supervisor_role', False)
    if hasattr(user, 'staffprofile'):
        return getattr(user.staffprofile, 'is_supervisor_role', False)
    return EmployeeEmployment.objects.filter(supervisor=user, status=EmployeeEmployment.Status.ACTIVE).exists()


def can_manage_hr(user):
    return is_hr_user(user) or is_ceo_user(user)


def get_supervisee_queryset(user):
    employment_ids = EmployeeEmployment.objects.filter(supervisor=user).values_list('user_id', flat=True)
    appraisal_ids = PerformanceAppraisal.objects.filter(supervisor=user).values_list('staff_id', flat=True)
    return User.objects.filter(Q(pk__in=employment_ids) | Q(pk__in=appraisal_ids), is_active=True).distinct()


def can_view_appraisal(user, appraisal):
    return is_hr_user(user) or appraisal.staff == user or appraisal.supervisor == user


def can_view_attendance_sheet(request_user, target_user):
    if is_hr_user(request_user) or is_ceo_user(request_user):
        return True
    if request_user == target_user:
        return True
    if is_supervisor_user(request_user):
        return get_supervisee_queryset(request_user).filter(pk=target_user.pk).exists()
    return False



# ── HR workflow stage notifications ─────────────────────────────────────────

ICT_STAGE_GROUPS = ['ICT', 'IT', 'Superuser of IT']
CEO_STAGE_GROUPS = ['CEO', 'Chief Executive Officer']


def _users_for_groups(group_names, fallback_superusers=True):
    """
    Return active users in the given workflow groups.
    If the group is not configured yet, fall back to superusers so the workflow
    still notifies someone during setup.
    """
    users = User.objects.filter(
        is_active=True,
        groups__name__in=group_names,
    ).distinct()

    if not users.exists() and fallback_superusers:
        users = User.objects.filter(is_active=True, is_superuser=True).distinct()

    return users


def _notification_type_system(CoreNotification):
    try:
        return CoreNotification.Type.SYSTEM
    except Exception:
        return 'system'


def _push_realtime_notification(user, title, message, link, icon='bi-person-lines-fill', color='#0f4c81'):
    """
    Push to the existing EasyOffice notification websocket group.
    Safe: does nothing if Channels/Redis is not available.
    """
    try:
        from channels.layers import get_channel_layer
        from asgiref.sync import async_to_sync

        layer = get_channel_layer()
        if not layer:
            return

        async_to_sync(layer.group_send)(
            f'notifications_{user.id}',
            {
                'type': 'send_notification',
                'data': {
                    'type': 'hr_onboarding',
                    'title': title,
                    'body': message,
                    'link': link,
                    'icon': icon,
                    'color': color,
                },
            },
        )
    except Exception:
        pass


def _store_stage_notification(user, sender, title, message, link):
    """
    Create persistent in-app bell notification using CoreNotification.
    Safe across slightly different CoreNotification implementations.
    """
    try:
        from apps.core.models import CoreNotification

        kwargs = {
            'recipient': user,
            'notification_type': _notification_type_system(CoreNotification),
            'title': title,
            'message': message,
            'link': link,
        }
        if sender:
            kwargs['sender'] = sender

        # Some EasyOffice versions include a JSON data field. Use it when present.
        try:
            field_names = {field.name for field in CoreNotification._meta.fields}
            if 'data' in field_names:
                kwargs['data'] = {
                    'module': 'hr',
                    'workflow': 'employee_onboarding',
                    'action_url': link,
                }
        except Exception:
            pass

        CoreNotification.objects.create(**kwargs)
    except Exception:
        pass


def _send_stage_email(user, subject, body, action_url):
    email = (getattr(user, 'email', '') or '').strip()
    if not email:
        try:
            email = (getattr(user.staffprofile, 'office_email', '') or '').strip()
        except Exception:
            email = ''
    if not email:
        return False

    try:
        message = EmailMessage(
            subject=subject,
            body=(
                f'{body}\n\n'
                f'Open in EasyOffice: {action_url}\n\n'
                '— EasyOffice HR'
            ),
            from_email=getattr(settings, 'DEFAULT_FROM_EMAIL', None),
            to=[email],
        )
        message.send(fail_silently=False)
        return True
    except Exception:
        return False


def notify_onboarding_stage(request, onboarding, stage, sender=None):
    """
    Notify the role that currently owns the onboarding stage.

    stage='ict' → ICT/IT account setup
    stage='ceo' → CEO approval review
    """
    sender = sender or getattr(request, 'user', None)
    link = f'/hr/onboarding/{onboarding.pk}/'
    try:
        action_url = request.build_absolute_uri(link)
    except Exception:
        action_url = link

    employee_name = onboarding.full_name
    employee_code = onboarding.employee_code or 'Pending code'
    job_title = onboarding.job_title_display or onboarding.job_title or 'Role pending'
    department = onboarding.department_display or onboarding.department or 'Department pending'

    if stage == 'ict':
        recipients = _users_for_groups(ICT_STAGE_GROUPS)
        title = f'ICT action required: setup account for {employee_name}'
        body = (
            f'HR has created an onboarding record for {employee_name} ({employee_code}). '
            f'The workflow is now with ICT to confirm the username and official email address.'
        )
        icon = 'bi-pc-display-horizontal'
        color = '#2563eb'
    elif stage == 'ceo':
        recipients = _users_for_groups(CEO_STAGE_GROUPS)
        title = f'CEO approval required: {employee_name}'
        body = (
            f'HR has submitted the hire request for {employee_name} ({employee_code}). '
            f'Role: {job_title}. Department: {department}. Please review, approve, request rework, or decline.'
        )
        icon = 'bi-award-fill'
        color = '#b7791f'
    else:
        return 0

    sent_to = 0
    for recipient in recipients:
        _store_stage_notification(recipient, sender, title, body, link)
        _push_realtime_notification(recipient, title, body, link, icon=icon, color=color)
        _send_stage_email(recipient, title, body, action_url)
        sent_to += 1

    return sent_to


def _client_ip(request):
    forwarded = request.META.get('HTTP_X_FORWARDED_FOR')
    if forwarded:
        return forwarded.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR')


def notify_hr_public_onboarding_submitted(request, onboarding):
    """
    Notify HR that an employee has submitted the public onboarding form.
    """
    link = reverse('employee_onboarding_detail', kwargs={'pk': onboarding.pk})
    try:
        action_url = request.build_absolute_uri(link)
    except Exception:
        action_url = link

    title = f'Employee onboarding submitted: {onboarding.full_name}'
    body = (
        f'{onboarding.full_name} has completed the public onboarding form. '
        f'HR should now review the information, complete the hire details, and submit to CEO.'
    )

    recipients = _users_for_groups(['HR'], fallback_superusers=True)

    sent = 0
    for user in recipients:
        _store_stage_notification(user, onboarding.created_by, title, body, link)
        _push_realtime_notification(
            user,
            title,
            body,
            link,
            icon='bi-person-check-fill',
            color='#0d9488',
        )
        _send_stage_email(user, title, body, action_url)
        sent += 1

    return sent


def send_onboarding_invite_email(request, invite):
    public_url = request.build_absolute_uri(
        reverse('employee_onboarding_public', kwargs={'token': invite.token})
    )

    subject = 'Employee Onboarding Form - EasyOffice HR'
    body = f"""
Dear {invite.candidate_name or 'Candidate'},

HR has invited you to complete your employee onboarding form.

Please use the secure link below:

{public_url}

This link is temporary and will expire on {invite.expires_at.strftime('%d %B %Y at %H:%M')}.

Important:
- This link can only be submitted once.
- Please complete your personal details, work history, references, emergency contacts, and upload the requested documents.

{invite.hr_message or ''}

Best regards,
EasyOffice HR
""".strip()

    email = EmailMessage(
        subject=subject,
        body=body,
        from_email=getattr(settings, 'DEFAULT_FROM_EMAIL', None),
        to=[invite.candidate_email],
    )
    email.send(fail_silently=False)
    return public_url


def send_employee_welcome_offer(request, onboarding):
    """
    Send welcome/offer message after CEO approval.
    The contract signing itself can be handled by your existing Files/Signature workflow.
    """
    recipients = []
    if onboarding.personal_email:
        recipients.append(onboarding.personal_email)
    if onboarding.official_email and onboarding.official_email not in recipients:
        recipients.append(onboarding.official_email)

    if not recipients:
        return False

    contract_due = onboarding.start_date or timezone.now().date()
    onboarding.contract_due_at = contract_due
    onboarding.contract_note = (
        'Employee must review the welcome offer and sign the employment contract '
        'before the official start date.'
    )
    onboarding.welcome_sent_at = timezone.now()
    onboarding.status = EmployeeOnboarding.Status.CONTRACT_PENDING
    onboarding.save(update_fields=[
        'contract_due_at',
        'contract_note',
        'welcome_sent_at',
        'status',
        'updated_at',
    ])

    subject = f'Welcome to EasyOffice - Offer Approved for {onboarding.full_name}'

    body = f"""
Dear {onboarding.full_name},

Congratulations. Your employment offer has been approved.

Offer Summary
-------------
Employee Code: {onboarding.employee_code}
Job Title: {onboarding.job_title_display or onboarding.job_title or 'To be confirmed'}
Department: {onboarding.department_display or onboarding.department or 'To be confirmed'}
Start Date: {onboarding.start_date or 'To be confirmed'}
Salary: {onboarding.salary}

Next Step
---------
Please review your welcome offer and sign your employment contract before your starting date.

Contract signing deadline: {contract_due}

HR will share the contract signing link with you. Your onboarding process will be closed once the contract is signed.

Welcome onboard.

Best regards,
EasyOffice HR
""".strip()

    email = EmailMessage(
        subject=subject,
        body=body,
        from_email=getattr(settings, 'DEFAULT_FROM_EMAIL', None),
        to=recipients,
    )
    email.send(fail_silently=False)
    return True

def parse_allowances_from_post(post):
    """
    Accepts fields like allowance_transport=500, allowance_housing=1000.
    Stores them as JSON: {"transport": "500.00", "housing": "1000.00"}
    """
    allowances = {}
    for key, value in post.items():
        if key.startswith('allowance_') and value not in ['', None]:
            name = key.replace('allowance_', '').strip().replace(' ', '_')
            allowances[name] = str(parse_decimal(value))
    return allowances


def approved_leave_for_day(staff, target_date):
    return LeaveRequest.objects.filter(
        status='approved',
        start_date__lte=target_date,
        end_date__gte=target_date,
        staff=staff,
    ).select_related('leave_type').first()


def leave_is_paid(leave_request):
    if not leave_request:
        return False
    leave_type = getattr(leave_request, 'leave_type', None)
    if not leave_type:
        return True
    return getattr(leave_type, 'is_paid', True)


def _money_fmt(value):
    return f"{Decimal(str(value or 0)):,.2f}"


def _read_shared_pdf_bytes(shared_file):
    """Read PDF bytes from a SharedFile safely."""
    if not shared_file or not getattr(shared_file, 'file', None):
        return b''
    shared_file.file.open('rb')
    try:
        return shared_file.file.read()
    finally:
        shared_file.file.close()


def get_default_payslip_letterhead():
    """
    Reuse the invoice app's default/shared letterhead for payslips.

    Priority:
    1. InvoiceTemplate named like payslip/payroll/default/letterhead and locked to invoice or any doc type.
    2. Latest shared/default InvoiceTemplate with a letterhead.
    3. Latest SharedFile PDF named letterhead/invoice/company.
    """
    try:
        from apps.invoices.models import InvoiceTemplate, DocType

        base = InvoiceTemplate.objects.select_related('letterhead').filter(
            letterhead__isnull=False,
        )
        invoice_or_open = Q(doc_type='') | Q(doc_type=DocType.INVOICE)

        preferred = base.filter(
            invoice_or_open,
            Q(name__icontains='payslip') |
            Q(name__icontains='payroll') |
            Q(name__icontains='default') |
            Q(name__icontains='letterhead') |
            Q(name__icontains='invoice'),
        ).order_by('-updated_at').first()

        template = preferred or base.filter(invoice_or_open).order_by('-updated_at').first() or base.order_by('-updated_at').first()
        if template:
            return template.letterhead
    except Exception:
        pass

    try:
        from apps.files.models import SharedFile
        return SharedFile.objects.filter(
            is_latest=True,
            name__iendswith='.pdf',
        ).filter(
            Q(name__icontains='letterhead') |
            Q(name__icontains='invoice') |
            Q(name__icontains='company')
        ).order_by('-created_at').first()
    except Exception:
        return None


def _first_pdf_page_size(pdf_bytes):
    """Return the first page size in points; defaults to A4 if parsing fails."""
    from reportlab.lib.pagesizes import A4
    try:
        try:
            from pypdf import PdfReader
        except Exception:
            from PyPDF2 import PdfReader
        reader = PdfReader(BytesIO(pdf_bytes))
        page = reader.pages[0]
        return float(page.mediabox.width), float(page.mediabox.height)
    except Exception:
        return A4


def _draw_wrapped_text(c, text, x, y, max_width, font='Helvetica', size=8.5, leading=11, color=None, max_lines=2):
    """Small canvas word wrapper. Returns the next y position."""
    from reportlab.pdfbase.pdfmetrics import stringWidth
    if color is not None:
        c.setFillColor(color)
    words = str(text or '—').split()
    lines = []
    line = ''
    for word in words:
        candidate = f'{line} {word}'.strip()
        if stringWidth(candidate, font, size) <= max_width:
            line = candidate
        else:
            if line:
                lines.append(line)
            line = word
    if line:
        lines.append(line)
    if not lines:
        lines = ['—']
    if max_lines and len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1].rstrip('.') + '…'
    c.setFont(font, size)
    for line in lines:
        c.drawString(x, y, line)
        y -= leading
    return y


def _make_payslip_overlay(record, page_width, page_height):
    """Create the centered payslip content overlay as a single-page PDF."""
    try:
        from reportlab.lib import colors
        from reportlab.pdfgen import canvas
    except Exception as exc:
        raise ValueError('ReportLab is required to generate payslip PDFs. Install it with: pip install reportlab') from exc

    buffer = BytesIO()
    c = canvas.Canvas(buffer, pagesize=(page_width, page_height))

    staff_name = record.staff.get_full_name() or record.staff.username
    employment = record.employment
    department = getattr(employment, 'department_display', '') if employment else ''
    position = getattr(employment, 'job_title_display', '') if employment else ''
    employee_code = getattr(employment, 'employee_code', '—') if employment else '—'
    period = f'{record.period_month:02d}/{record.period_year}'
    currency = 'GMD'

    navy = colors.HexColor('#0b1f3a')
    blue = colors.HexColor('#0f4c81')
    gold = colors.HexColor('#d99a29')
    slate = colors.HexColor('#475569')
    light = colors.HexColor('#f8fafc')
    soft_border = colors.HexColor('#d9e2ec')
    white = colors.white

    card_w = min(page_width * 0.74, 455)
    card_h = min(page_height * 0.74, 575)
    x = (page_width - card_w) / 2
    y = (page_height - card_h) / 2

    # Subtle shadow + translucent card so letterhead remains visible behind the payslip.
    try:
        c.setFillAlpha(0.12)
    except Exception:
        pass
    c.setFillColor(colors.black)
    c.roundRect(x + 4, y - 5, card_w, card_h, 18, fill=1, stroke=0)
    try:
        c.setFillAlpha(0.92)
    except Exception:
        pass
    c.setFillColor(white)
    c.roundRect(x, y, card_w, card_h, 18, fill=1, stroke=0)
    try:
        c.setFillAlpha(1)
    except Exception:
        pass

    header_h = 74
    c.setFillColor(navy)
    c.roundRect(x, y + card_h - header_h, card_w, header_h, 18, fill=1, stroke=0)
    c.rect(x, y + card_h - header_h, card_w, 20, fill=1, stroke=0)
    c.setFillColor(gold)
    c.rect(x, y + card_h - header_h, 5, header_h, fill=1, stroke=0)

    c.setFillColor(white)
    c.setFont('Helvetica-Bold', 22)
    c.drawString(x + 22, y + card_h - 38, 'PAYSLIP')
    c.setFont('Helvetica', 8.5)
    c.drawString(x + 24, y + card_h - 56, 'Confidential payroll statement generated by EasyOffice HR')

    c.setFillColor(colors.HexColor('#e8f1ff'))
    c.roundRect(x + card_w - 132, y + card_h - 52, 104, 26, 11, fill=1, stroke=0)
    c.setFillColor(navy)
    c.setFont('Helvetica-Bold', 9)
    c.drawCentredString(x + card_w - 80, y + card_h - 43, f'PERIOD {period}')

    current_y = y + card_h - header_h - 24

    c.setFillColor(light)
    c.roundRect(x + 20, current_y - 80, card_w - 40, 78, 10, fill=1, stroke=0)
    c.setStrokeColor(soft_border)
    c.roundRect(x + 20, current_y - 80, card_w - 40, 78, 10, fill=0, stroke=1)

    label_color = colors.HexColor('#64748b')
    value_color = colors.HexColor('#0f172a')
    left_x = x + 36
    right_x = x + card_w / 2 + 12
    row_y = current_y - 18

    def label_value(label, value, px, py, width):
        c.setFillColor(label_color)
        c.setFont('Helvetica-Bold', 6.8)
        c.drawString(px, py, str(label).upper())
        return _draw_wrapped_text(c, value, px, py - 12, width, font='Helvetica-Bold', size=9, leading=10.5, color=value_color, max_lines=2)

    label_value('Employee', staff_name, left_x, row_y, (card_w / 2) - 50)
    label_value('Employee Code', employee_code, right_x, row_y, (card_w / 2) - 48)
    label_value('Department', department or '—', left_x, row_y - 38, (card_w / 2) - 50)
    label_value('Position', position or '—', right_x, row_y - 38, (card_w / 2) - 48)

    current_y -= 104

    net_box_h = 70
    c.setFillColor(colors.HexColor('#eef7f1'))
    c.roundRect(x + 20, current_y - net_box_h, card_w - 40, net_box_h, 12, fill=1, stroke=0)
    c.setStrokeColor(colors.HexColor('#b7dfc4'))
    c.roundRect(x + 20, current_y - net_box_h, card_w - 40, net_box_h, 12, fill=0, stroke=1)

    c.setFillColor(colors.HexColor('#166534'))
    c.setFont('Helvetica-Bold', 9)
    c.drawString(x + 38, current_y - 23, 'NET PAY')
    c.setFont('Helvetica-Bold', 24)
    c.drawString(x + 38, current_y - 51, f'{currency} {_money_fmt(record.net_salary)}')

    c.setFillColor(slate)
    c.setFont('Helvetica', 8)
    c.drawRightString(x + card_w - 38, current_y - 25, f'Status: {record.get_status_display()}')
    c.drawRightString(x + card_w - 38, current_y - 42, f'Payment Date: {record.payment_date or "—"}')
    c.drawRightString(x + card_w - 38, current_y - 59, f'Ref: {record.payment_reference or "—"}')

    current_y -= 94

    col_gap = 14
    col_w = (card_w - 40 - col_gap) / 2
    left_col = x + 20
    right_col = left_col + col_w + col_gap
    table_h = 160

    def panel(px, py, title, rows, accent):
        c.setFillColor(white)
        c.roundRect(px, py - table_h, col_w, table_h, 10, fill=1, stroke=0)
        c.setStrokeColor(soft_border)
        c.roundRect(px, py - table_h, col_w, table_h, 10, fill=0, stroke=1)
        c.setFillColor(accent)
        c.roundRect(px, py - 26, col_w, 26, 10, fill=1, stroke=0)
        c.rect(px, py - 26, col_w, 10, fill=1, stroke=0)
        c.setFillColor(white)
        c.setFont('Helvetica-Bold', 8.6)
        c.drawString(px + 12, py - 17, title)
        ry = py - 45
        for label, value, strong in rows:
            c.setFillColor(value_color if strong else slate)
            c.setFont('Helvetica-Bold' if strong else 'Helvetica', 7.5 if not strong else 8.2)
            c.drawString(px + 12, ry, label)
            c.drawRightString(px + col_w - 12, ry, value)
            ry -= 21 if strong else 18
            if strong:
                c.setStrokeColor(colors.HexColor('#e2e8f0'))
                c.line(px + 12, ry + 9, px + col_w - 12, ry + 9)

    gross = money(record.gross_salary)
    net = money(record.net_salary)
    earnings_rows = [
        ('Basic Salary', _money_fmt(record.basic_salary), False),
        ('Allowances', _money_fmt((record.allowances or {}).get('paid_total', 0)), False),
        ('Leave Payout', _money_fmt(record.leave_payout), False),
        ('Gross Salary', _money_fmt(record.gross_salary), True),
    ]
    deduction_rows = [
        ('Tax Deducted', _money_fmt(record.tax_deducted), False),
        ('Other Deductions', _money_fmt((record.deductions or {}).get('other_total', 0)), False),
        ('Total Deductions', _money_fmt(gross - net), True),
    ]
    panel(left_col, current_y, 'EARNINGS', earnings_rows, blue)
    panel(right_col, current_y, 'DEDUCTIONS', deduction_rows, colors.HexColor('#7f1d1d'))

    current_y -= table_h + 24

    chip_titles = [
        ('Work Days', record.total_work_days),
        ('Payable', record.payable_days),
        ('Absent', record.absent_days),
        ('Half Days', record.half_days),
        ('Unpaid Leave', record.unpaid_leave_days),
    ]
    chip_gap = 7
    chip_w = (card_w - 40 - (chip_gap * 4)) / 5
    chip_h = 46
    for idx, (title, value) in enumerate(chip_titles):
        px = x + 20 + idx * (chip_w + chip_gap)
        c.setFillColor(colors.HexColor('#f8fafc'))
        c.roundRect(px, current_y - chip_h, chip_w, chip_h, 9, fill=1, stroke=0)
        c.setStrokeColor(soft_border)
        c.roundRect(px, current_y - chip_h, chip_w, chip_h, 9, fill=0, stroke=1)
        c.setFillColor(label_color)
        c.setFont('Helvetica-Bold', 5.9)
        c.drawCentredString(px + chip_w / 2, current_y - 16, title.upper())
        c.setFillColor(navy)
        c.setFont('Helvetica-Bold', 12)
        c.drawCentredString(px + chip_w / 2, current_y - 34, str(value or '0'))

    footer_y = y + 24
    c.setStrokeColor(colors.HexColor('#e2e8f0'))
    c.line(x + 22, footer_y + 18, x + card_w - 22, footer_y + 18)
    c.setFillColor(slate)
    c.setFont('Helvetica-Oblique', 7.2)
    c.drawCentredString(x + card_w / 2, footer_y + 3, 'This is a system-generated payslip. For questions, contact HR Payroll.')

    c.showPage()
    c.save()
    return buffer.getvalue()


def _merge_letterhead_and_overlay(letterhead_bytes, overlay_bytes):
    """Merge the overlay on top of the first page of a PDF letterhead."""
    try:
        try:
            from pypdf import PdfReader, PdfWriter
        except Exception:
            from PyPDF2 import PdfReader, PdfWriter

        letter_reader = PdfReader(BytesIO(letterhead_bytes))
        overlay_reader = PdfReader(BytesIO(overlay_bytes))
        page = letter_reader.pages[0]
        page.merge_page(overlay_reader.pages[0])
        writer = PdfWriter()
        writer.add_page(page)
        output = BytesIO()
        writer.write(output)
        return output.getvalue()
    except Exception:
        return overlay_bytes


def build_payslip_pdf(record):
    """
    Return a single-page payslip PDF using the invoice letterhead as background.
    """
    letterhead = get_default_payslip_letterhead()
    letterhead_bytes = _read_shared_pdf_bytes(letterhead)
    page_width, page_height = _first_pdf_page_size(letterhead_bytes) if letterhead_bytes else _first_pdf_page_size(b'')
    overlay_bytes = _make_payslip_overlay(record, page_width, page_height)

    if letterhead_bytes:
        return _merge_letterhead_and_overlay(letterhead_bytes, overlay_bytes)
    return overlay_bytes

def payslip_filename(record):
    staff_name = (record.staff.get_full_name() or record.staff.username or 'staff').replace(' ', '_')
    return f'payslip_{staff_name}_{record.period_year}_{record.period_month:02d}.pdf'


def send_payslip_email(record):
    recipient = record.staff.email
    try:
        profile = record.staff.staffprofile
        recipient = recipient or profile.office_email
    except Exception:
        pass

    if not recipient:
        raise ValueError('This staff member has no email address for payslip delivery.')

    pdf_bytes = build_payslip_pdf(record)
    subject = f'Payslip for {record.period_month}/{record.period_year}'
    body = (
        f'Dear {record.staff.get_full_name() or record.staff.username},\n\n'
        f'Please find attached your payslip for {record.period_month}/{record.period_year}.\n\n'
        'Best regards,\nHR Payroll'
    )
    email = EmailMessage(
        subject=subject,
        body=body,
        from_email=getattr(settings, 'DEFAULT_FROM_EMAIL', None),
        to=[recipient],
    )
    email.attach(payslip_filename(record), pdf_bytes, 'application/pdf')
    email.send(fail_silently=False)
    record.mark_payslip_sent(recipient)
    return recipient


def calculate_payroll_for_employment(employment, year, month, generated_by=None):
    setting = HRSetting.get_solo()
    work_days = setting.working_days_in_month(year, month)
    total_work_days = Decimal(str(len(work_days) or 1))

    period_start = date(year, month, 1)
    period_end = date(year, month, monthrange(year, month)[1])

    daily_salary = money(Decimal(str(employment.monthly_salary or 0)) / total_work_days)
    daily_allowance = money(employment.allowance_total() / total_work_days)

    attendance_map = {
        rec.date: rec
        for rec in AttendanceRecord.objects.filter(
            staff=employment.user,
            date__range=[period_start, period_end]
        )
    }

    discipline_items = list(
        DisciplinaryAction.objects.filter(
            staff=employment.user,
            status=DisciplinaryAction.Status.ACTIVE,
        ).filter(
            Q(start_date__isnull=True) | Q(start_date__lte=period_end),
            Q(end_date__isnull=True) | Q(end_date__gte=period_start),
        )
    )

    payable_days = Decimal('0.00')
    absent_days = Decimal('0.00')
    half_days = Decimal('0.00')
    unpaid_leave_days = Decimal('0.00')

    for work_day in work_days:
        if employment.start_date and work_day < employment.start_date:
            continue
        if employment.end_date and work_day > employment.end_date:
            continue
        if employment.resignation_date and work_day > employment.resignation_date:
            continue

        if employment.salary_paused or any(item.affects_date(work_day) for item in discipline_items):
            unpaid_leave_days += Decimal('1.00')
            continue

        rec = attendance_map.get(work_day)
        factor = Decimal('1.00')

        if rec:
            if rec.status in [AttendanceRecord.Status.PRESENT, AttendanceRecord.Status.REMOTE, AttendanceRecord.Status.PAID_LEAVE, AttendanceRecord.Status.LEAVE, AttendanceRecord.Status.HOLIDAY]:
                factor = Decimal('1.00')
            elif rec.status == AttendanceRecord.Status.LATE:
                if setting.discipline_enabled and rec.minutes_late >= setting.late_half_day_after_minutes:
                    factor = Decimal('0.50')
                    half_days += Decimal('1.00')
                else:
                    factor = Decimal('1.00')
            elif rec.status == AttendanceRecord.Status.HALF_DAY:
                factor = Decimal('0.50')
                half_days += Decimal('1.00')
            elif rec.status in [AttendanceRecord.Status.UNPAID_LEAVE, AttendanceRecord.Status.SUSPENDED]:
                factor = Decimal('0.00')
                unpaid_leave_days += Decimal('1.00')
            else:
                factor = Decimal('0.00')
                absent_days += Decimal('1.00')
        else:
            leave = approved_leave_for_day(employment.user, work_day)
            if leave:
                factor = Decimal('1.00') if leave_is_paid(leave) else Decimal('0.00')
                if factor == Decimal('0.00'):
                    unpaid_leave_days += Decimal('1.00')
            elif setting.missing_attendance_is_absent:
                factor = Decimal('0.00')
                absent_days += Decimal('1.00')
            else:
                factor = Decimal('1.00')

        payable_days += factor

    base_pay = money(daily_salary * payable_days)
    allowance_pay = money(daily_allowance * payable_days)
    leave_payout = Decimal('0.00')

    if employment.resignation_date and period_start <= employment.resignation_date <= period_end:
        leave_payout = money(daily_salary * Decimal(str(employment.leave_balance_days or 0)))

    gross = money(base_pay + allowance_pay + leave_payout)
    deductions = {
        'absent_days': str(absent_days),
        'half_days': str(half_days),
        'unpaid_leave_days': str(unpaid_leave_days),
    }

    record, _ = PayrollRecord.objects.update_or_create(
        staff=employment.user,
        period_month=month,
        period_year=year,
        defaults={
            'employment': employment,
            'basic_salary': base_pay,
            'allowances': {'monthly_total': str(employment.allowance_total()), 'paid_total': str(allowance_pay)},
            'deductions': deductions,
            'gross_salary': gross,
            'net_salary': gross,
            'tax_deducted': Decimal('0.00'),
            'total_work_days': total_work_days,
            'payable_days': payable_days,
            'absent_days': absent_days,
            'half_days': half_days,
            'unpaid_leave_days': unpaid_leave_days,
            'leave_payout': leave_payout,
            'generated_by': generated_by,
            'generated_at': timezone.now(),
            'notes': 'Auto-generated from HR employment, attendance, leave and discipline records.',
        }
    )
    return record


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
        ctx['is_ceo_user'] = is_ceo_user(user)
        ctx['is_it_user'] = is_it_user(user)
        ctx['is_supervisor_user'] = is_supervisor_user(user)
        ctx['is_staff_user'] = not is_hr_user(user) and not is_supervisor_user(user)

        if is_hr_user(user) or is_ceo_user(user):
            ctx['active_staff_count'] = EmployeeEmployment.objects.filter(status=EmployeeEmployment.Status.ACTIVE).count()
            ctx['pending_onboarding_count'] = EmployeeOnboarding.objects.exclude(status__in=[EmployeeOnboarding.Status.HIRED, EmployeeOnboarding.Status.DECLINED, EmployeeOnboarding.Status.COMPLETED]).count()
            ctx['ceo_review_count'] = EmployeeOnboarding.objects.filter(status=EmployeeOnboarding.Status.CEO_REVIEW).count()
            ctx['it_setup_count'] = EmployeeOnboarding.objects.filter(status=EmployeeOnboarding.Status.IT_SETUP).count()
            ctx['discipline_active_count'] = DisciplinaryAction.objects.filter(status=DisciplinaryAction.Status.ACTIVE).count()

            ctx['appraisals_this_year'] = PerformanceAppraisal.objects.filter(year=year).count()
            ctx['pending_appraisals'] = PerformanceAppraisal.objects.filter(
                year=year,
                status__in=['draft', 'self_review', 'supervisor_review', 'hr_review']
            ).count()
            ctx['payroll_this_month'] = PayrollRecord.objects.filter(period_year=year, period_month=month).count()
            ctx['attendance_today_count'] = AttendanceRecord.objects.filter(date=today).count()
            ctx['present_today_count'] = AttendanceRecord.objects.filter(date=today, status__in=['present', 'late', 'remote']).count()
            ctx['late_today_count'] = AttendanceRecord.objects.filter(date=today, status__in=['late', 'half_day']).count()
            ctx['absent_today_count'] = AttendanceRecord.objects.filter(date=today, status='absent').count()

            ctx['recent_onboardings'] = EmployeeOnboarding.objects.select_related('hire_category', 'supervisor').order_by('-created_at')[:8]
            ctx['recent_appraisals'] = PerformanceAppraisal.objects.select_related('staff', 'supervisor').order_by('-created_at')[:8]
            ctx['recent_attendance'] = AttendanceRecord.objects.select_related('staff', 'marked_by').order_by('-date', 'staff__last_name')[:10]

        elif is_supervisor_user(user):
            supervisees = get_supervisee_queryset(user)
            ctx['supervisee_count'] = supervisees.count()
            ctx['pending_appraisals'] = PerformanceAppraisal.objects.filter(year=year, supervisor=user, status__in=['supervisor_review', 'hr_review']).count()
            ctx['attendance_today_count'] = AttendanceRecord.objects.filter(date=today, staff__in=supervisees).count()
            ctx['recent_appraisals'] = PerformanceAppraisal.objects.filter(Q(supervisor=user) | Q(staff=user)).select_related('staff', 'supervisor').order_by('-created_at')[:8]
            ctx['recent_attendance'] = AttendanceRecord.objects.filter(staff__in=supervisees).select_related('staff', 'marked_by').order_by('-date', 'staff__last_name')[:10]
        else:
            ctx['my_appraisals'] = PerformanceAppraisal.objects.filter(staff=user).select_related('supervisor').order_by('-year')[:8]
            ctx['my_attendance_today'] = AttendanceRecord.objects.filter(staff=user, date=today).first()
            ctx['my_attendance_this_month'] = AttendanceRecord.objects.filter(staff=user, date__year=year, date__month=month).order_by('-date')[:10]
            ctx['my_payroll_this_month'] = PayrollRecord.objects.filter(staff=user, period_year=year, period_month=month).first()

        return ctx


class EmployeeOnboardingListView(LoginRequiredMixin, TemplateView):
    template_name = 'hr/onboarding_list.html'

    def dispatch(self, request, *args, **kwargs):
        if not (is_hr_user(request.user) or is_ceo_user(request.user) or is_it_user(request.user)):
            return HttpResponseForbidden('You do not have permission to view onboarding records.')
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        q = self.request.GET.get('q', '').strip()
        status = self.request.GET.get('status', '').strip()
        records = EmployeeOnboarding.objects.select_related('hire_category', 'department_ref', 'unit', 'position', 'location', 'supervisor', 'staff_user').order_by('-created_at')
        if q:
            records = records.filter(
                Q(first_name__icontains=q) | Q(last_name__icontains=q) | Q(employee_code__icontains=q) |
                Q(official_email__icontains=q) | Q(proposed_username__icontains=q)
            )
        if status:
            records = records.filter(status=status)
        ctx.update({
            'records': records,
            'q': q,
            'status': status,
            'status_choices': EmployeeOnboarding.Status.choices,
            'is_hr_user': is_hr_user(self.request.user),
            'is_ceo_user': is_ceo_user(self.request.user),
            'is_it_user': is_it_user(self.request.user),
        })
        return ctx


class EmployeeOnboardingCreateView(LoginRequiredMixin, TemplateView):
    template_name = 'hr/onboarding_form.html'

    def dispatch(self, request, *args, **kwargs):
        if not is_hr_user(request.user):
            return HttpResponseForbidden('Only HR can create employee onboarding records.')
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['categories'] = HireCategory.objects.filter(is_active=True)
        ctx['departments'] = Department.objects.filter(is_active=True).order_by('name')
        ctx['units'] = Unit.objects.filter(is_active=True).select_related('department').order_by('department__name', 'name')
        ctx['positions'] = Position.objects.filter(is_active=True).select_related('department').order_by('title')
        ctx['locations'] = OfficeLocation.objects.filter(is_active=True).order_by('name')
        ctx['supervisors'] = User.objects.filter(is_active=True).order_by('first_name', 'last_name')
        return ctx

    def post(self, request, *args, **kwargs):
        onboarding = EmployeeOnboarding.objects.create(
            first_name=request.POST.get('first_name', '').strip(),
            middle_name=request.POST.get('middle_name', '').strip(),
            last_name=request.POST.get('last_name', '').strip(),
            gender=request.POST.get('gender', ''),
            date_of_birth=parse_date(request.POST.get('date_of_birth')),
            nationality=request.POST.get('nationality', '').strip(),
            personal_email=request.POST.get('personal_email', '').strip(),
            phone=request.POST.get('phone', '').strip(),
            alternative_phone=request.POST.get('alternative_phone', '').strip(),
            address_line=request.POST.get('address_line', '').strip(),
            city=request.POST.get('city', '').strip(),
            region=request.POST.get('region', '').strip(),
            country=request.POST.get('country', 'The Gambia').strip() or 'The Gambia',
            hr_notes=request.POST.get('hr_notes', '').strip(),
            created_by=request.user,
        )
        notify_onboarding_stage(request, onboarding, 'ict', sender=request.user)
        messages.success(request, f'Employee record created. Username suggestion: {onboarding.proposed_username}. IT can now update the official email.')
        return redirect('employee_onboarding_detail', pk=onboarding.pk)

class EmployeeOnboardingInviteCreateView(LoginRequiredMixin, View):
    """
    HR creates a temporary one-time public onboarding link.
    The employee fills only their own sections.
    """

    def post(self, request):
        if not is_hr_user(request.user):
            return HttpResponseForbidden('Only HR can create onboarding links.')

        candidate_email = request.POST.get('candidate_email', '').strip()
        candidate_name = request.POST.get('candidate_name', '').strip()
        expires_days = int(request.POST.get('expires_days') or 7)
        hr_message = request.POST.get('hr_message', '').strip()

        if not candidate_email:
            messages.error(request, 'Candidate email is required.')
            return redirect('employee_onboarding_list')

        expires_days = max(1, min(expires_days, 30))

        invite = EmployeeOnboardingInvite.objects.create(
            candidate_name=candidate_name,
            candidate_email=candidate_email,
            hr_message=hr_message,
            expires_at=timezone.now() + timedelta(days=expires_days),
            created_by=request.user,
        )

        try:
            public_url = send_onboarding_invite_email(request, invite)
            messages.success(request, f'Onboarding link sent to {candidate_email}.')
            messages.info(request, f'Public link: {public_url}')
        except Exception as exc:
            messages.warning(
                request,
                f'Invite created but email could not be sent: {exc}'
            )

        return redirect('employee_onboarding_list')

class PublicEmployeeOnboardingView(View):
    """
    Public no-login onboarding form.

    Employee fills:
    - Personal identity and contact details
    - Address
    - Documents
    - Work history
    - References
    - Emergency contacts

    HR later fills:
    - Hire category
    - Department/unit/position/location
    - Supervisor
    - Start/end date
    - Salary/allowances
    - HR notes
    - Submit to CEO
    """

    template_name = 'hr/public_onboarding_form.html'

    def get_invite(self, token):
        return get_object_or_404(EmployeeOnboardingInvite, token=token)

    def get(self, request, token):
        invite = self.get_invite(token)

        if not invite.can_submit:
            return render(request, 'hr/public_onboarding_invalid.html', {
                'invite': invite,
            })

        return render(request, self.template_name, {
            'invite': invite,
            'gender_choices': EmployeeOnboarding.Gender.choices,
            'document_types': EmployeeDocument.DocumentType.choices,
        })

    @transaction.atomic
    def post(self, request, token):
        invite = get_object_or_404(
            EmployeeOnboardingInvite.objects.select_for_update(),
            token=token,
        )

        if not invite.can_submit:
            return render(request, 'hr/public_onboarding_invalid.html', {
                'invite': invite,
            })

        first_name = request.POST.get('first_name', '').strip()
        last_name = request.POST.get('last_name', '').strip()
        personal_email = request.POST.get('personal_email', '').strip()

        if not first_name or not last_name or not personal_email:
            messages.error(request, 'First name, last name, and personal email are required.')
            return render(request, self.template_name, {
                'invite': invite,
                'gender_choices': EmployeeOnboarding.Gender.choices,
                'document_types': EmployeeDocument.DocumentType.choices,
            })

        onboarding = EmployeeOnboarding.objects.create(
            first_name=first_name,
            middle_name=request.POST.get('middle_name', '').strip(),
            last_name=last_name,
            gender=request.POST.get('gender', '').strip(),
            date_of_birth=parse_date(request.POST.get('date_of_birth')),
            nationality=request.POST.get('nationality', '').strip(),
            personal_email=personal_email,
            phone=request.POST.get('phone', '').strip(),
            alternative_phone=request.POST.get('alternative_phone', '').strip(),
            address_line=request.POST.get('address_line', '').strip(),
            city=request.POST.get('city', '').strip(),
            region=request.POST.get('region', '').strip(),
            country=request.POST.get('country', 'The Gambia').strip() or 'The Gambia',
            status=EmployeeOnboarding.Status.HR_REVIEW,
            hr_notes='Submitted by employee through temporary onboarding link. HR should review and complete hire details.',
            created_by=invite.created_by,
        )

        invite.onboarding = onboarding
        invite.submitted_at = timezone.now()
        invite.submitted_ip = _client_ip(request)
        invite.submitted_user_agent = request.META.get('HTTP_USER_AGENT', '')[:1000]
        invite.save(update_fields=[
            'onboarding',
            'submitted_at',
            'submitted_ip',
            'submitted_user_agent',
        ])

        # Documents
        document_files = request.FILES.getlist('document_files')
        document_titles = request.POST.getlist('document_titles')
        document_types = request.POST.getlist('document_types')

        for index, upload in enumerate(document_files):
            if not upload:
                continue

            title = ''
            doc_type = EmployeeDocument.DocumentType.OTHER

            if index < len(document_titles):
                title = document_titles[index].strip()
            if index < len(document_types) and document_types[index]:
                doc_type = document_types[index]

            EmployeeDocument.objects.create(
                onboarding=onboarding,
                document_type=doc_type,
                title=title or upload.name,
                file=upload,
                uploaded_by=invite.created_by,
            )

        # Work history
        employers = request.POST.getlist('work_employer')
        work_titles = request.POST.getlist('work_job_title')
        work_start_dates = request.POST.getlist('work_start_date')
        work_end_dates = request.POST.getlist('work_end_date')
        work_responsibilities = request.POST.getlist('work_responsibilities')

        for index, employer in enumerate(employers):
            employer = employer.strip()
            if not employer:
                continue

            EmployeeWorkHistory.objects.create(
                onboarding=onboarding,
                employer=employer,
                job_title=work_titles[index].strip() if index < len(work_titles) else '',
                start_date=parse_date(work_start_dates[index]) if index < len(work_start_dates) else None,
                end_date=parse_date(work_end_dates[index]) if index < len(work_end_dates) else None,
                responsibilities=work_responsibilities[index].strip() if index < len(work_responsibilities) else '',
            )

        # References
        ref_names = request.POST.getlist('ref_name')
        ref_relationships = request.POST.getlist('ref_relationship')
        ref_organizations = request.POST.getlist('ref_organization')
        ref_phones = request.POST.getlist('ref_phone')
        ref_emails = request.POST.getlist('ref_email')
        ref_notes = request.POST.getlist('ref_notes')

        for index, name in enumerate(ref_names):
            name = name.strip()
            if not name:
                continue

            EmployeeReference.objects.create(
                onboarding=onboarding,
                name=name,
                relationship=ref_relationships[index].strip() if index < len(ref_relationships) else '',
                organization=ref_organizations[index].strip() if index < len(ref_organizations) else '',
                phone=ref_phones[index].strip() if index < len(ref_phones) else '',
                email=ref_emails[index].strip() if index < len(ref_emails) else '',
                notes=ref_notes[index].strip() if index < len(ref_notes) else '',
            )

        # Emergency contacts
        contact_names = request.POST.getlist('contact_name')
        contact_relationships = request.POST.getlist('contact_relationship')
        contact_phones = request.POST.getlist('contact_phone')
        contact_alt_phones = request.POST.getlist('contact_alt_phone')
        contact_addresses = request.POST.getlist('contact_address')

        for index, name in enumerate(contact_names):
            name = name.strip()
            phone = contact_phones[index].strip() if index < len(contact_phones) else ''

            if not name or not phone:
                continue

            EmployeeEmergencyContact.objects.create(
                onboarding=onboarding,
                name=name,
                relationship=contact_relationships[index].strip() if index < len(contact_relationships) else '',
                phone=phone,
                alternative_phone=contact_alt_phones[index].strip() if index < len(contact_alt_phones) else '',
                address=contact_addresses[index].strip() if index < len(contact_addresses) else '',
            )

        notify_hr_public_onboarding_submitted(request, onboarding)

        return render(request, 'hr/public_onboarding_done.html', {
            'invite': invite,
            'onboarding': onboarding,
        })

class EmployeeOnboardingDetailView(LoginRequiredMixin, DetailView):
    model = EmployeeOnboarding
    template_name = 'hr/onboarding_detail.html'
    context_object_name = 'onboarding'

    def dispatch(self, request, *args, **kwargs):
        if not (is_hr_user(request.user) or is_ceo_user(request.user) or is_it_user(request.user)):
            return HttpResponseForbidden('You do not have permission to view this onboarding record.')
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['categories'] = HireCategory.objects.filter(is_active=True)
        ctx['departments'] = Department.objects.filter(is_active=True).order_by('name')
        ctx['units'] = Unit.objects.filter(is_active=True).select_related('department').order_by('department__name', 'name')
        ctx['positions'] = Position.objects.filter(is_active=True).select_related('department').order_by('title')
        ctx['locations'] = OfficeLocation.objects.filter(is_active=True).order_by('name')
        ctx['supervisors'] = User.objects.filter(is_active=True).order_by('first_name', 'last_name')
        ctx['is_hr_user'] = is_hr_user(self.request.user)
        ctx['is_ceo_user'] = is_ceo_user(self.request.user)
        ctx['is_it_user'] = is_it_user(self.request.user)
        ctx['document_types'] = EmployeeDocument.DocumentType.choices
        return ctx

    def post(self, request, pk):
        onboarding = get_object_or_404(EmployeeOnboarding, pk=pk)
        action = request.POST.get('action')

        try:
            if action == 'it_update':
                if not is_it_user(request.user):
                    return HttpResponseForbidden('Only IT/superuser can update official account details.')
                onboarding.mark_account_ready(
                    request.user,
                    official_email=request.POST.get('official_email', '').strip(),
                    username=request.POST.get('proposed_username', '').strip() or None,
                )
                messages.success(request, 'Official email and username updated by IT.')

            elif action in ['save_hire', 'save_and_submit_for_ceo']:
                if not is_hr_user(request.user):
                    return HttpResponseForbidden('Only HR can update hire information.')

                onboarding.hire_category_id = request.POST.get('hire_category') or None
                onboarding.department_ref_id = request.POST.get('department_ref') or None
                onboarding.unit_id = request.POST.get('unit') or None
                onboarding.position_id = request.POST.get('position') or None
                onboarding.location_id = request.POST.get('location') or None

                # Keep legacy text labels populated for old templates/search/backward compatibility.
                if onboarding.department_ref_id:
                    dept = Department.objects.filter(pk=onboarding.department_ref_id).first()
                    onboarding.department = dept.name if dept else request.POST.get('department', '').strip()
                else:
                    onboarding.department = request.POST.get('department', '').strip()

                if onboarding.position_id:
                    pos = Position.objects.filter(pk=onboarding.position_id).first()
                    onboarding.job_title = pos.title if pos else request.POST.get('job_title', '').strip()
                else:
                    onboarding.job_title = request.POST.get('job_title', '').strip()

                onboarding.supervisor_id = request.POST.get('supervisor') or None
                onboarding.start_date = parse_date(request.POST.get('start_date'))
                onboarding.end_date = parse_date(request.POST.get('end_date'))
                onboarding.day_hire_days = int(request.POST.get('day_hire_days') or 0)
                onboarding.salary = parse_decimal(request.POST.get('salary'))
                onboarding.allowances = parse_allowances_from_post(request.POST)
                if onboarding.status in [EmployeeOnboarding.Status.REWORK, EmployeeOnboarding.Status.IT_SETUP]:
                    onboarding.status = EmployeeOnboarding.Status.HIRE_DRAFT
                onboarding.save()

                if action == 'save_and_submit_for_ceo':
                    previous_status = onboarding.status
                    onboarding.submit_for_ceo(request.user)
                    if previous_status != EmployeeOnboarding.Status.CEO_REVIEW:
                        notify_onboarding_stage(request, onboarding, 'ceo', sender=request.user)
                    messages.success(request, 'Hire information saved and submitted to CEO for approval.')
                else:
                    messages.success(request, 'Hire information saved.')

            elif action == 'upload_document':
                if not is_hr_user(request.user):
                    return HttpResponseForbidden('Only HR can upload onboarding documents.')
                upload = request.FILES.get('file')
                if upload:
                    EmployeeDocument.objects.create(
                        onboarding=onboarding,
                        document_type=request.POST.get('document_type') or EmployeeDocument.DocumentType.OTHER,
                        title=request.POST.get('title') or upload.name,
                        file=upload,
                        uploaded_by=request.user,
                    )
                    messages.success(request, 'Document uploaded.')

            elif action == 'add_work_history':
                if not is_hr_user(request.user):
                    return HttpResponseForbidden('Only HR can add work history.')
                EmployeeWorkHistory.objects.create(
                    onboarding=onboarding,
                    employer=request.POST.get('employer', '').strip(),
                    job_title=request.POST.get('history_job_title', '').strip(),
                    start_date=parse_date(request.POST.get('history_start_date')),
                    end_date=parse_date(request.POST.get('history_end_date')),
                    responsibilities=request.POST.get('responsibilities', '').strip(),
                )
                messages.success(request, 'Work history added.')

            elif action == 'add_reference':
                if not is_hr_user(request.user):
                    return HttpResponseForbidden('Only HR can add references.')
                EmployeeReference.objects.create(
                    onboarding=onboarding,
                    name=request.POST.get('reference_name', '').strip(),
                    relationship=request.POST.get('relationship', '').strip(),
                    organization=request.POST.get('organization', '').strip(),
                    phone=request.POST.get('reference_phone', '').strip(),
                    email=request.POST.get('reference_email', '').strip(),
                    notes=request.POST.get('reference_notes', '').strip(),
                )
                messages.success(request, 'Reference added.')

            elif action == 'add_emergency_contact':
                if not is_hr_user(request.user):
                    return HttpResponseForbidden('Only HR can add emergency contacts.')
                EmployeeEmergencyContact.objects.create(
                    onboarding=onboarding,
                    name=request.POST.get('contact_name', '').strip(),
                    relationship=request.POST.get('contact_relationship', '').strip(),
                    phone=request.POST.get('contact_phone', '').strip(),
                    alternative_phone=request.POST.get('contact_alt_phone', '').strip(),
                    address=request.POST.get('contact_address', '').strip(),
                )
                messages.success(request, 'Emergency contact added.')

            elif action == 'submit_for_ceo':
                if not is_hr_user(request.user):
                    return HttpResponseForbidden('Only HR can submit a hire request to CEO.')
                previous_status = onboarding.status
                onboarding.submit_for_ceo(request.user)
                if previous_status != EmployeeOnboarding.Status.CEO_REVIEW:
                    notify_onboarding_stage(request, onboarding, 'ceo', sender=request.user)
                messages.success(request, 'Hire request submitted to CEO for approval.')

            elif action == 'ceo_approve':
                if not is_ceo_user(request.user):
                    return HttpResponseForbidden('Only CEO/superuser can approve hire requests.')
                onboarding.ceo_comments = request.POST.get('ceo_comments', '').strip()
                onboarding.save(update_fields=['ceo_comments', 'updated_at'])
                staff_user = onboarding.approve_and_create_staff(request.user)
                send_employee_welcome_offer(request, onboarding)

                # Auto-generate the offer letter PDF — attach to onboarding
                # AND save into the Files app under HR / Offer Letters.
                offer_letter_msg = ''
                try:
                    save_offer_letter_for_onboarding(onboarding, request.user)
                    offer_letter_msg = ' Offer letter PDF generated and saved.'
                except Exception as exc:
                    offer_letter_msg = (
                        f' Note: offer letter PDF could not be generated automatically '
                        f'({exc}). You can retry from the Generate Offer Letter button.'
                    )

                messages.success(
                    request,
                    f'Hire approved, staff account created for '
                    f'{staff_user.get_full_name() or staff_user.username}, '
                    f'and welcome/offer message sent.{offer_letter_msg}'
                )

            elif action == 'generate_offer_letter':
                # Manual (re)generate. HR or CEO only, and only after CEO approval.
                if not (is_hr_user(request.user) or is_ceo_user(request.user)):
                    return HttpResponseForbidden('Only HR or CEO can generate offer letters.')
                if onboarding.status not in [
                    EmployeeOnboarding.Status.APPROVED,
                    EmployeeOnboarding.Status.HIRED,
                    EmployeeOnboarding.Status.CONTRACT_PENDING,
                ]:
                    messages.error(
                        request,
                        'The offer letter can only be generated after the CEO has approved the hire.'
                    )
                else:
                    try:
                        save_offer_letter_for_onboarding(onboarding, request.user)
                        messages.success(request, 'Offer letter regenerated and saved.')
                    except Exception as exc:
                        messages.error(request, f'Offer letter generation failed: {exc}')

            elif action == 'ceo_rework':
                if not is_ceo_user(request.user):
                    return HttpResponseForbidden('Only CEO/superuser can request rework.')
                onboarding.status = EmployeeOnboarding.Status.REWORK
                onboarding.ceo_comments = request.POST.get('ceo_comments', '').strip()
                onboarding.save(update_fields=['status', 'ceo_comments', 'updated_at'])
                messages.warning(request, 'Hire request sent back to HR for rework.')

            elif action == 'ceo_decline':
                if not is_ceo_user(request.user):
                    return HttpResponseForbidden('Only CEO/superuser can decline hire requests.')
                onboarding.status = EmployeeOnboarding.Status.DECLINED
                onboarding.decline_reason = request.POST.get('decline_reason', '').strip()
                onboarding.save(update_fields=['status', 'decline_reason', 'updated_at'])
                messages.error(request, 'Hire request declined.')

            elif action == 'mark_offer_letter_signed':
                if not (is_hr_user(request.user) or is_ceo_user(request.user)):
                    return HttpResponseForbidden(
                        'Only HR or CEO can confirm the offer letter has been signed.'
                    )
                signed = request.POST.get('signed', '1') in ['1', 'true', 'on', 'yes']
                onboarding.mark_offer_letter_signed(request.user, signed=signed)
                if signed:
                    messages.success(request, 'Offer letter marked as signed.')
                else:
                    messages.info(request, 'Offer letter signature cleared.')

            elif action == 'mark_contract_signed':
                if not (is_hr_user(request.user) or is_ceo_user(request.user)):
                    return HttpResponseForbidden(
                        'Only HR or CEO can confirm the contract has been signed.'
                    )
                signed = request.POST.get('signed', '1') in ['1', 'true', 'on', 'yes']
                onboarding.mark_contract_signed(request.user, signed=signed)
                if signed:
                    messages.success(request, 'Employment contract marked as signed.')
                else:
                    messages.info(request, 'Contract signature cleared.')

            elif action == 'mark_hiring_complete':
                if not (is_hr_user(request.user) or is_ceo_user(request.user)):
                    return HttpResponseForbidden(
                        'Only HR or CEO can mark hiring complete.'
                    )
                onboarding.mark_hiring_complete(request.user)
                messages.success(
                    request,
                    f'Hiring complete for {onboarding.full_name}. '
                    f'The onboarding record is now closed.'
                )

        except ValueError as exc:
            messages.error(request, str(exc))

        except IntegrityError as exc:
            messages.error(
                request,
                'The staff account could not be linked because it is already connected to another onboarding or employment record. '
                'Please check the official email, proposed username, and existing staff record before approving again.'
            )

        return redirect('employee_onboarding_detail', pk=onboarding.pk)




class OfferLetterDownloadView(LoginRequiredMixin, View):
    """
    Stream the latest generated offer letter PDF for an onboarding record.

    Use ?inline=1 to display in the browser; default is download.
    Falls back to building the PDF on the fly if no EmployeeDocument is saved
    yet (so a freshly-approved record without a stored PDF still works).
    """

    def get(self, request, pk):
        if not (is_hr_user(request.user) or is_ceo_user(request.user)):
            return HttpResponseForbidden('You do not have permission to view this offer letter.')

        onboarding = get_object_or_404(EmployeeOnboarding, pk=pk)

        # Prefer the saved EmployeeDocument so the URL matches what HR sees in
        # the documents table; build on the fly only if nothing is saved.
        saved = EmployeeDocument.objects.filter(
            onboarding=onboarding,
            document_type=EmployeeDocument.DocumentType.CONTRACT,
            title__startswith='Offer Letter',
        ).order_by('-uploaded_at').first()

        if saved and saved.file:
            saved.file.open('rb')
            try:
                pdf_bytes = saved.file.read()
            finally:
                saved.file.close()
        else:
            try:
                pdf_bytes = build_offer_letter_pdf(onboarding)
            except Exception as exc:
                messages.error(request, f'Offer letter could not be built: {exc}')
                return redirect('employee_onboarding_detail', pk=onboarding.pk)

        disposition = 'inline' if request.GET.get('inline') else 'attachment'
        response = HttpResponse(pdf_bytes, content_type='application/pdf')
        response['Content-Disposition'] = f'{disposition}; filename="{offer_letter_filename(onboarding)}"'
        return response


class OfferLetterSendForSigningView(LoginRequiredMixin, View):
    """
    POST-only: email the offer letter to the employee with a digital
    signing link via the Files-app signature flow.

    Generates (or regenerates) the offer letter PDF, saves it as an
    EmployeeDocument + Files-app SharedFile, creates a token-based
    SignatureRequest with the acceptance signature/date fields pre-placed
    on the "Accepted by Employee" lines, and emails the employee the offer
    PDF with a "Review & Sign" link (no login required).

    When the employee signs, apps.files.tasks.finalise_signature_after_sign
    calls apps.hr.offer_letter.handle_offer_signature_completed, which marks
    this onboarding record as offer-letter-signed and replaces the offer
    letter under "Documents, Work History & References" with the stamped,
    signed PDF.
    """

    http_method_names = ['post']

    def post(self, request, pk):
        if not (is_hr_user(request.user) or is_ceo_user(request.user)):
            return HttpResponseForbidden(
                'Only HR or CEO can send the offer letter for signing.'
            )

        onboarding = get_object_or_404(EmployeeOnboarding, pk=pk)

        if onboarding.offer_letter_signed:
            messages.info(
                request,
                'The offer letter has already been signed — nothing to send.'
            )
            return redirect('employee_onboarding_detail', pk=onboarding.pk)

        # Same gate as manual generation: only after CEO approval.
        if onboarding.status not in [
            EmployeeOnboarding.Status.APPROVED,
            EmployeeOnboarding.Status.HIRED,
            EmployeeOnboarding.Status.CONTRACT_PENDING,
        ]:
            messages.error(
                request,
                'The offer letter can only be sent after the CEO has approved the hire.'
            )
            return redirect('employee_onboarding_detail', pk=onboarding.pk)

        try:
            expires_days = int(request.POST.get('expires_days') or 14)
        except (TypeError, ValueError):
            expires_days = 14

        try:
            _doc, _shared, sig_req, signer = send_offer_letter_for_signature(
                onboarding,
                request.user,
                base_url=request.build_absolute_uri('/'),
                expires_days=expires_days,
                extra_message=(request.POST.get('message') or '').strip(),
            )
        except ValueError as exc:
            # User-friendly failures raised by the helper
            # (no email on record, Files app unavailable, ...).
            messages.error(request, str(exc))
            return redirect('employee_onboarding_detail', pk=onboarding.pk)
        except Exception as exc:
            messages.error(
                request,
                f'Something went wrong while sending the offer for signing ({exc}). '
                f'Please try again.'
            )
            return redirect('employee_onboarding_detail', pk=onboarding.pk)

        messages.success(
            request,
            f'Offer letter emailed to {signer.email} with a signing link '
            f'(valid {expires_days} days). This record will be marked as '
            f'signed automatically once '
            f'{onboarding.first_name or "the employee"} accepts and signs.'
        )
        return redirect('employee_onboarding_detail', pk=onboarding.pk)


class EmploymentListView(LoginRequiredMixin, TemplateView):
    template_name = 'hr/employment_list.html'

    def dispatch(self, request, *args, **kwargs):
        if not can_manage_hr(request.user):
            return HttpResponseForbidden('You do not have permission to view staff employment records.')
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        q = self.request.GET.get('q', '').strip()
        records = EmployeeEmployment.objects.select_related('user', 'hire_category', 'department_ref', 'unit', 'position', 'location', 'supervisor').order_by('user__first_name')
        if q:
            records = records.filter(Q(user__first_name__icontains=q) | Q(user__last_name__icontains=q) | Q(employee_code__icontains=q) | Q(department__icontains=q) | Q(department_ref__name__icontains=q) | Q(position__title__icontains=q))
        ctx['records'] = records
        ctx['q'] = q
        return ctx


class EmploymentActionView(LoginRequiredMixin, View):
    def post(self, request, pk):
        if not can_manage_hr(request.user):
            return HttpResponseForbidden('You do not have permission to update employment records.')
        employment = get_object_or_404(EmployeeEmployment, pk=pk)
        action = request.POST.get('action')

        if action == 'resign':
            employment.resign(
                resignation_date=parse_date(request.POST.get('resignation_date')) or timezone.now().date(),
                notice_given_date=parse_date(request.POST.get('notice_given_date')) or timezone.now().date(),
                notes=request.POST.get('notes', '').strip(),
            )
            messages.warning(request, 'Staff marked as resigned. Salary will stop from the resignation date and leave balance will be paid out.')

        elif action == 'activate':
            employment.status = EmployeeEmployment.Status.ACTIVE
            employment.salary_paused = False
            employment.save(update_fields=['status', 'salary_paused', 'updated_at'])
            messages.success(request, 'Employment record reactivated.')

        return redirect('employment_list')


class DisciplinaryActionCreateView(LoginRequiredMixin, View):
    def post(self, request, staff_id):
        if not can_manage_hr(request.user):
            return HttpResponseForbidden('Only HR/CEO/superuser can create discipline records.')
        staff = get_object_or_404(User, pk=staff_id, is_active=True)
        action_type = request.POST.get('action_type')
        salary_paused = request.POST.get('salary_paused') == 'on'

        DisciplinaryAction.objects.create(
            staff=staff,
            action_type=action_type,
            reason=request.POST.get('reason', '').strip(),
            start_date=parse_date(request.POST.get('start_date')),
            end_date=parse_date(request.POST.get('end_date')),
            salary_paused=salary_paused,
            issued_by=request.user,
            notes=request.POST.get('notes', '').strip(),
        )

        if salary_paused:
            EmployeeEmployment.objects.filter(user=staff).update(salary_paused=True, status=EmployeeEmployment.Status.SUSPENDED)
        messages.success(request, 'Disciplinary action recorded.')
        return redirect('employment_list')


class AppraisalListView(LoginRequiredMixin, TemplateView):
    template_name = 'hr/appraisal_list.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        user = self.request.user
        year = int(self.request.GET.get('year', timezone.now().year))

        if is_hr_user(user):
            appraisals = PerformanceAppraisal.objects.filter(year=year)
        elif is_supervisor_user(user):
            appraisals = PerformanceAppraisal.objects.filter(year=year).filter(Q(supervisor=user) | Q(staff=user))
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
        if not can_manage_hr(request.user):
            return HttpResponseForbidden('You do not have permission to view payroll.')
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        year = int(self.request.GET.get('year', timezone.now().year))
        month = int(self.request.GET.get('month', timezone.now().month))
        records = PayrollRecord.objects.filter(period_year=year, period_month=month).select_related('staff', 'staff__staffprofile', 'employment', 'employment__department_ref', 'employment__position').order_by('staff__last_name')
        ctx['records'] = records
        ctx['year'] = year
        ctx['month'] = month
        ctx['total_gross'] = records.aggregate(total=Sum('gross_salary'))['total'] or 0
        ctx['total_net'] = records.aggregate(total=Sum('net_salary'))['total'] or 0
        ctx['can_approve_payroll'] = can_manage_hr(self.request.user)
        return ctx


class PayrollGenerateView(LoginRequiredMixin, View):
    def post(self, request):
        if not can_manage_hr(request.user):
            return HttpResponseForbidden('You do not have permission to generate payroll.')
        year = int(request.POST.get('year') or timezone.now().year)
        month = int(request.POST.get('month') or timezone.now().month)
        employments = EmployeeEmployment.objects.select_related('user').filter(
            status__in=[EmployeeEmployment.Status.ACTIVE, EmployeeEmployment.Status.ON_LEAVE, EmployeeEmployment.Status.RESIGNED]
        )
        count = 0
        created_records = []
        with transaction.atomic():
            for employment in employments:
                record = calculate_payroll_for_employment(employment, year, month, generated_by=request.user)
                created_records.append(record)
                count += 1

        sent_count = 0
        if request.POST.get('send_payslips') == 'on':
            for record in created_records:
                try:
                    send_payslip_email(record)
                    sent_count += 1
                except Exception as exc:
                    record.mark_payslip_error(exc)
            messages.success(request, f'Payroll generated for {count} staff record(s). Payslips emailed to {sent_count} staff member(s).')
        else:
            messages.success(request, f'Payroll generated for {count} staff record(s).')
        return redirect(f'/hr/payroll/?year={year}&month={month}')


class PayrollActionView(LoginRequiredMixin, View):
    def post(self, request, pk):
        if not can_manage_hr(request.user):
            return HttpResponseForbidden('You do not have permission to update payroll.')
        record = get_object_or_404(PayrollRecord, pk=pk)
        action = request.POST.get('action')
        if action == 'approve':
            record.approve(request.user)
            messages.success(request, 'Payroll record approved.')
        elif action == 'mark_paid':
            record.mark_paid(request.user, reference=request.POST.get('payment_reference', '').strip())
            if request.POST.get('send_payslip') == 'on':
                try:
                    recipient = send_payslip_email(record)
                    messages.success(request, f'Payroll record marked as paid and payslip emailed to {recipient}.')
                except Exception as exc:
                    record.mark_payslip_error(exc)
                    messages.warning(request, f'Payroll record marked as paid, but payslip email failed: {exc}')
            else:
                messages.success(request, 'Payroll record marked as paid.')
        elif action == 'email_payslip':
            try:
                recipient = send_payslip_email(record)
                messages.success(request, f'Payslip emailed to {recipient}.')
            except Exception as exc:
                record.mark_payslip_error(exc)
                messages.error(request, f'Payslip email failed: {exc}')
        elif action == 'cancel':
            record.status = PayrollRecord.Status.CANCELLED
            record.save(update_fields=['status'])
            messages.warning(request, 'Payroll record cancelled.')
        return redirect(f'/hr/payroll/?year={record.period_year}&month={record.period_month}')


class PayrollPayslipPrintView(LoginRequiredMixin, View):
    def get(self, request, pk):
        if not can_manage_hr(request.user):
            return HttpResponseForbidden('You do not have permission to print payslips.')
        record = get_object_or_404(
            PayrollRecord.objects.select_related('staff', 'staff__staffprofile', 'employment', 'employment__department_ref', 'employment__position'),
            pk=pk,
        )
        pdf_bytes = build_payslip_pdf(record)
        response = HttpResponse(pdf_bytes, content_type='application/pdf')
        response['Content-Disposition'] = f'inline; filename="{payslip_filename(record)}"'
        return response


class PayrollBulkPayslipEmailView(LoginRequiredMixin, View):
    def post(self, request):
        if not can_manage_hr(request.user):
            return HttpResponseForbidden('You do not have permission to email payslips.')
        year = int(request.POST.get('year') or timezone.now().year)
        month = int(request.POST.get('month') or timezone.now().month)
        records = PayrollRecord.objects.filter(period_year=year, period_month=month).select_related('staff', 'staff__staffprofile', 'employment')
        sent_count = 0
        for record in records:
            try:
                send_payslip_email(record)
                sent_count += 1
            except Exception as exc:
                record.mark_payslip_error(exc)
        messages.success(request, f'Payslips emailed to {sent_count} staff member(s).')
        return redirect(f'/hr/payroll/?year={year}&month={month}')


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
        selected_date = parse_date(date_str) or timezone.now().date()

        staff_qs = User.objects.filter(is_active=True)
        if hasattr(User, 'status'):
            staff_qs = staff_qs.filter(status='active')
        staff_qs = staff_qs.order_by('first_name', 'last_name')
        if q:
            staff_qs = staff_qs.filter(Q(first_name__icontains=q) | Q(last_name__icontains=q) | Q(email__icontains=q) | Q(username__icontains=q))

        summary = AttendanceRecord.objects.filter(date=selected_date)
        attendance_map = {str(rec.staff_id): rec for rec in summary.select_related('staff')}
        approved_leaves = LeaveRequest.objects.filter(status='approved', start_date__lte=selected_date, end_date__gte=selected_date, staff__is_active=True).select_related('staff', 'leave_type')
        leave_map = {str(lr.staff_id): lr for lr in approved_leaves}

        discipline_map = {}
        for da in DisciplinaryAction.objects.filter(status=DisciplinaryAction.Status.ACTIVE).filter(Q(start_date__isnull=True) | Q(start_date__lte=selected_date), Q(end_date__isnull=True) | Q(end_date__gte=selected_date)):
            if da.affects_date(selected_date):
                discipline_map[str(da.staff_id)] = da

        staff_rows = []
        for staff in staff_qs:
            record = attendance_map.get(str(staff.id))
            leave_request = leave_map.get(str(staff.id))
            discipline = discipline_map.get(str(staff.id))
            derived_leave_record = None
            if not record and leave_request:
                derived_leave_record = {'status': 'paid_leave' if leave_is_paid(leave_request) else 'unpaid_leave', 'notes': leave_request.reason}
            if not record and discipline:
                derived_leave_record = {'status': 'unpaid_leave', 'notes': discipline.reason}
            staff_rows.append({'staff': staff, 'record': record, 'leave_request': leave_request, 'discipline': discipline, 'derived_leave_record': derived_leave_record})

        ctx.update({
            'selected_date': selected_date,
            'staff_rows': staff_rows,
            'attendance_statuses': AttendanceRecord.Status.choices,
            'q': q,
            'summary': summary,
            'summary_count': summary.count(),
            'present_count': summary.filter(status__in=['present', 'late', 'remote']).count(),
            'late_count': summary.filter(status__in=['late', 'half_day']).count(),
            'leave_count': approved_leaves.count() + summary.filter(status__in=['paid_leave', 'leave']).count(),
            'absent_count': summary.filter(status='absent').count(),
        })
        return ctx

    def post(self, request, *args, **kwargs):
        if not is_hr_user(request.user):
            messages.error(request, 'Only HR can manage staff attendance.')
            return redirect('hr_dashboard')
        selected_date = parse_date(request.POST.get('date')) or timezone.now().date()
        setting = HRSetting.get_solo()

        approved_leave_staff_ids = set(LeaveRequest.objects.filter(status='approved', start_date__lte=selected_date, end_date__gte=selected_date).values_list('staff_id', flat=True))

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
            if staff.id in approved_leave_staff_ids and status == 'present':
                status = AttendanceRecord.Status.PAID_LEAVE
            record, _ = AttendanceRecord.objects.get_or_create(staff=staff, date=selected_date, defaults={'status': status, 'marked_by': request.user, 'notes': notes})
            record.status = status
            record.notes = notes
            record.marked_by = request.user
            record.check_in = timezone.datetime.strptime(check_in, '%H:%M').time() if check_in else None
            record.check_out = timezone.datetime.strptime(check_out, '%H:%M').time() if check_out else None
            record.apply_time_policy(setting)
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
        year = int(self.request.GET.get('year', today.year))
        month = int(self.request.GET.get('month', today.month))
        if month < 1 or month > 12:
            month = today.month

        staff = get_object_or_404(User, pk=target_staff_id, is_active=True) if target_staff_id else request_user
        if not can_view_attendance_sheet(request_user, staff):
            raise PermissionDenied('You do not have permission to generate this attendance sheet.')

        start_date = timezone.datetime(year, month, 1).date()
        end_date = timezone.datetime(year, month, monthrange(year, month)[1]).date()
        records_qs = AttendanceRecord.objects.filter(staff=staff, date__range=[start_date, end_date]).order_by('date')
        record_map = {rec.date: rec for rec in records_qs}

        days = []
        present_count = late_count = absent_count = leave_count = remote_count = 0
        total_hours = Decimal('0.00')
        for day_num in range(1, monthrange(year, month)[1] + 1):
            current_date = timezone.datetime(year, month, day_num).date()
            rec = record_map.get(current_date)
            if rec:
                total_hours += Decimal(str(rec.worked_hours or 0))
                if rec.status == 'present':
                    present_count += 1
                elif rec.status in ['late', 'half_day']:
                    late_count += 1
                elif rec.status == 'absent':
                    absent_count += 1
                elif rec.status in ['leave', 'paid_leave', 'unpaid_leave']:
                    leave_count += 1
                elif rec.status == 'remote':
                    remote_count += 1
            days.append({'date': current_date, 'record': rec})

        if is_hr_user(request_user):
            selectable_staff = User.objects.filter(is_active=True).order_by('first_name', 'last_name')
        elif is_supervisor_user(request_user):
            supervisees = get_supervisee_queryset(request_user)
            selectable_staff = User.objects.filter(Q(pk=request_user.pk) | Q(pk__in=supervisees.values_list('pk', flat=True)), is_active=True).distinct().order_by('first_name', 'last_name')
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
            'month_choices': [(1, 'January'), (2, 'February'), (3, 'March'), (4, 'April'), (5, 'May'), (6, 'June'), (7, 'July'), (8, 'August'), (9, 'September'), (10, 'October'), (11, 'November'), (12, 'December')],
            'year_choices': range(today.year, today.year - 5, -1),
            'can_manage_attendance': is_hr_user(request_user),
            'is_hr_user': is_hr_user(request_user),
            'is_supervisor_user': is_supervisor_user(request_user),
        })
        return ctx


class ZKDeviceListView(LoginRequiredMixin, TemplateView):
    template_name = 'hr/zk_devices.html'

    def dispatch(self, request, *args, **kwargs):
        if not is_hr_user(request.user):
            messages.error(request, 'Only HR can manage attendance devices.')
            return redirect('hr_dashboard')
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['devices'] = ZKDevice.objects.all().select_related('location')
        return ctx

    def post(self, request, *args, **kwargs):
        if not is_hr_user(request.user):
            return redirect('hr_dashboard')
        device_id = request.POST.get('device_id')
        if device_id:
            device = get_object_or_404(ZKDevice, pk=device_id)
            device.name = request.POST.get('name', device.name).strip()
            device.ip_address = request.POST.get('ip_address', device.ip_address).strip()
            device.port = int(request.POST.get('port') or device.port)
            device.comm_password = int(request.POST.get('comm_password') or 0)
            device.timeout = int(request.POST.get('timeout') or device.timeout)
            device.force_udp = request.POST.get('force_udp') == 'on'
            device.clear_device_after_sync = request.POST.get('clear_device_after_sync') == 'on'
            device.is_active = request.POST.get('is_active') == 'on'
            device.save()
            messages.success(request, f'Device "{device.name}" updated.')
        else:
            device = ZKDevice.objects.create(
                name=request.POST.get('name', 'New Terminal').strip(),
                ip_address=request.POST.get('ip_address', '').strip(),
                port=int(request.POST.get('port') or 4370),
                comm_password=int(request.POST.get('comm_password') or 0),
                timeout=int(request.POST.get('timeout') or 10),
                force_udp=request.POST.get('force_udp') == 'on',
                clear_device_after_sync=request.POST.get('clear_device_after_sync') == 'on',
                is_active=request.POST.get('is_active') == 'on',
                created_by=request.user,
            )
            messages.success(request, f'Device "{device.name}" added.')
        return redirect('zk_device_list')


class ZKDeviceTestView(LoginRequiredMixin, View):
    def post(self, request, pk, *args, **kwargs):
        if not is_hr_user(request.user):
            return redirect('hr_dashboard')
        device = get_object_or_404(ZKDevice, pk=pk)
        try:
            with ZKClient(device) as client:
                info = client.test_connection()
            device.last_sync_status = 'Connection OK: ' + ', '.join(
                f'{k}={v}' for k, v in info.items()
            )
            device.save(update_fields=['last_sync_status'])
            messages.success(request, f'Connected to {device.name}. {device.last_sync_status}')
        except ZKConnectionError as exc:
            messages.error(request, f'Connection failed: {exc}')
        return redirect('zk_device_list')


class ZKDeviceSyncView(LoginRequiredMixin, View):
    def post(self, request, pk, *args, **kwargs):
        if not is_hr_user(request.user):
            return redirect('hr_dashboard')
        device = get_object_or_404(ZKDevice, pk=pk)
        try:
            summary = zk_service.sync_device(device, marked_by=request.user)
            messages.success(
                request,
                f'{device.name}: imported {summary["imported"]} punches, '
                f'created {summary["records_created"]} and updated '
                f'{summary["records_updated"]} attendance records. '
                f'Unmapped enroll IDs: {summary["unmapped"]}.'
            )
        except ZKConnectionError as exc:
            messages.error(request, f'Sync failed: {exc}')
        return redirect('zk_device_list')


class ZKSyncAllView(LoginRequiredMixin, View):
    def post(self, request, *args, **kwargs):
        if not is_hr_user(request.user):
            return redirect('hr_dashboard')
        summary = zk_service.sync_all_active_devices(marked_by=request.user)
        messages.success(
            request,
            f'Synced all devices. Created {summary["records_created"]}, '
            f'updated {summary["records_updated"]} attendance records.'
        )
        return redirect('zk_device_list')


class ZKUserMapView(LoginRequiredMixin, TemplateView):
    """Map terminal enroll IDs to staff. Pulls suggestions from the device."""
    template_name = 'hr/zk_user_map.html'

    def dispatch(self, request, *args, **kwargs):
        if not is_hr_user(request.user):
            messages.error(request, 'Only HR can manage device mappings.')
            return redirect('hr_dashboard')
        self.device = get_object_or_404(ZKDevice, pk=kwargs['pk'])
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['device'] = self.device
        ctx['existing_maps'] = ZKUserMap.objects.filter(
            device=self.device
        ).select_related('staff')
        ctx['staff_list'] = User.objects.filter(is_active=True).order_by('first_name', 'last_name')
        if self.request.GET.get('load') == '1':
            try:
                ctx['suggestions'] = zk_service.import_users_as_maps(self.device)
            except ZKConnectionError as exc:
                messages.error(self.request, f'Could not read users from device: {exc}')
                ctx['suggestions'] = []
        return ctx

    def post(self, request, *args, **kwargs):
        action = request.POST.get('action')
        if action == 'delete':
            ZKUserMap.objects.filter(pk=request.POST.get('map_id'), device=self.device).delete()
            messages.success(request, 'Mapping removed.')
            return redirect('zk_user_map', pk=self.device.pk)

        # Bulk save: for every device_user_id field with a chosen staff, upsert.
        saved = 0
        for key, value in request.POST.items():
            if not key.startswith('map_staff_'):
                continue
            device_user_id = key.replace('map_staff_', '')
            if not value:
                continue
            staff = User.objects.filter(pk=value, is_active=True).first()
            if not staff:
                continue
            name = request.POST.get(f'map_name_{device_user_id}', '').strip()
            ZKUserMap.objects.update_or_create(
                device=self.device,
                device_user_id=device_user_id,
                defaults={'staff': staff, 'device_user_name': name},
            )
            saved += 1
        messages.success(request, f'Saved {saved} mapping(s).')
        # Re-link any previously unmapped punches now that maps exist.
        _relink_unmapped_punches(self.device)
        return redirect('zk_user_map', pk=self.device.pk)


def _relink_unmapped_punches(device):
    """After mapping is updated, attach staff to historical unmapped punches."""
    maps = {m.device_user_id: m.staff_id for m in ZKUserMap.objects.filter(device=device)}
    for device_user_id, staff_id in maps.items():
        ZKPunchLog.objects.filter(
            device=device, device_user_id=device_user_id, staff__isnull=True
        ).update(staff_id=staff_id, processed=False)