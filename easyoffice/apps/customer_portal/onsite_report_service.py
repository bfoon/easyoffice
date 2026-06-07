"""
apps/customer_portal/onsite_report_service.py
==============================================

NEW MODULE — builds the monthly service report PDF and wires it into the
existing CEO e-signature flow (finance.ContractSignatureRequest).

Public API
----------
  * find_visits_for_period(*, contract=None, customer_name=None,
                            period_start, period_end)
        → queryset of submitted/verified OnSiteVisitReport in range.

  * generate_monthly_report(*, scope, contract=None, customer_name=None,
                            period_start, period_end, actor)
        → MonthlyServiceReport with pdf_file written and pdf_hash set.

  * send_report_for_ceo_signature(report, *, ceo_name, ceo_email, actor)
        → wraps the report PDF in a ContractDocument + ContractSignatureRequest
          so the existing signing UI/flow signs it. Returns the sig request.

  * email_signed_report(report)
        → emails the signed PDF to the contract's PM contacts.

The PDF mirrors the layout of the uploaded April-2026 maintenance report:
  1. Header + reporting period
  2. Executive summary (counts table)
  3. Detailed activity log (the five-column table)
  4. Sign-off block (filled by CEO signature stamp)
"""
from __future__ import annotations

import hashlib
import io
import calendar
from collections import Counter
from datetime import date
from typing import Optional

from django.conf import settings
from django.core.files.base import ContentFile
from django.db.models import Q
from django.utils import timezone


class OnSiteReportError(Exception):
    pass


# ─────────────────────────────────────────────────────────────────────────────
# Branding (same source as contract_document_service._branding)
# ─────────────────────────────────────────────────────────────────────────────

def _branding():
    return {
        'org_name':  getattr(settings, 'OFFICE_NAME',  'Our Organisation'),
        'org_addr':  getattr(settings, 'OFFICE_ADDR',  ''),
        'org_phone': getattr(settings, 'OFFICE_PHONE', ''),
        'org_email': getattr(settings, 'OFFICE_EMAIL', ''),
    }


def month_bounds(year: int, month: int) -> tuple[date, date]:
    """First and last day of the given month."""
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, 1), date(year, month, last_day)


# ─────────────────────────────────────────────────────────────────────────────
# Visit selection
# ─────────────────────────────────────────────────────────────────────────────

def find_visits_for_period(*, contract=None, customer_name=None,
                           period_start: date, period_end: date):
    """
    Return submitted/verified visits whose visit_date falls in the period.

    Scope:
      * contract set        → that contract's visits + visits whose ticket
                              resolves to that contract.
      * customer_name set   → all visits across that customer's contracts.
    """
    from apps.customer_portal.models_onsite_report import OnSiteVisitReport
    from apps.finance.models import Contract

    qs = OnSiteVisitReport.objects.filter(
        visit_date__gte=period_start,
        visit_date__lte=period_end,
        status__in=[
            OnSiteVisitReport.Status.SUBMITTED,
            OnSiteVisitReport.Status.VERIFIED,
            OnSiteVisitReport.Status.REPORTED,
        ],
    )

    if contract is not None:
        qs = qs.filter(
            Q(contract=contract)
            | Q(ticket_assignment__ticket__contract=contract)
        )
    elif customer_name:
        contract_ids = list(
            Contract.objects
            .filter(counterparty_name__iexact=customer_name)
            .values_list('pk', flat=True)
        )
        qs = qs.filter(
            Q(contract_id__in=contract_ids)
            | Q(ticket_assignment__ticket__contract_id__in=contract_ids)
        )
    else:
        raise OnSiteReportError('Provide either a contract or a customer_name.')

    return qs.select_related('technician', 'contract').prefetch_related('activities').distinct()


# ─────────────────────────────────────────────────────────────────────────────
# Report generation
# ─────────────────────────────────────────────────────────────────────────────

def generate_monthly_report(*, scope: str, contract=None, customer_name: str = '',
                            period_start: date, period_end: date, actor=None):
    """
    Build (or rebuild) a MonthlyServiceReport for the period + scope.

    Returns the saved MonthlyServiceReport with its PDF written.
    """
    from apps.customer_portal.models_onsite_report import (
        MonthlyServiceReport, OnSiteVisitReport,
    )

    if scope == MonthlyServiceReport.Scope.CONTRACT and contract is None:
        raise OnSiteReportError('Contract scope requires a contract.')
    if scope == MonthlyServiceReport.Scope.CUSTOMER and not customer_name:
        if contract is not None:
            customer_name = contract.counterparty_name or ''
        if not customer_name:
            raise OnSiteReportError('Customer scope requires a customer_name.')

    visits = list(find_visits_for_period(
        contract=contract if scope == MonthlyServiceReport.Scope.CONTRACT else None,
        customer_name=customer_name if scope == MonthlyServiceReport.Scope.CUSTOMER else None,
        period_start=period_start,
        period_end=period_end,
    ))

    total_activities = sum(v.activity_count for v in visits)

    report = MonthlyServiceReport(
        scope=scope,
        status=MonthlyServiceReport.Status.DRAFT,
        contract=contract if scope == MonthlyServiceReport.Scope.CONTRACT else None,
        customer_name=customer_name,
        period_start=period_start,
        period_end=period_end,
        title=_default_title(scope, contract, customer_name, period_start),
        visit_ids=[str(v.pk) for v in visits],
        total_activities=total_activities,
        total_visits=len(visits),
        generated_by=actor,
    )

    pdf_bytes = _build_report_pdf(report, visits)
    base = (
        (contract.reference if contract else customer_name) or 'report'
    ).replace('/', '-').replace(' ', '_')
    report.pdf_file.save(
        f'{base}-{period_start:%Y-%m}.pdf',
        ContentFile(pdf_bytes),
        save=False,
    )
    report.pdf_hash = hashlib.sha256(pdf_bytes).hexdigest()
    report.save()

    # Mark the contributing visits as REPORTED
    if visits:
        OnSiteVisitReport.objects.filter(
            pk__in=[v.pk for v in visits]
        ).update(status=OnSiteVisitReport.Status.REPORTED)

    return report


def _default_title(scope, contract, customer_name, period_start):
    who = contract.title if contract else customer_name
    return f'Monthly Service Report — {who} — {period_start:%B %Y}'


# ─────────────────────────────────────────────────────────────────────────────
# PDF builder (matches the April-2026 maintenance report layout)
# ─────────────────────────────────────────────────────────────────────────────

def _build_report_pdf(report, visits) -> bytes:
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.units import cm
        from reportlab.lib import colors
        from reportlab.platypus import (
            SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
        )
        from reportlab.lib.enums import TA_CENTER, TA_LEFT
    except ImportError as e:
        raise OnSiteReportError(
            'ReportLab is not installed. Run: pip install reportlab'
        ) from e

    b = _branding()
    buf = io.BytesIO()
    page_w, page_h = A4
    inner_w = page_w - 4 * cm

    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=2 * cm, rightMargin=2 * cm,
        topMargin=2 * cm, bottomMargin=2 * cm,
        title=report.title or 'Monthly Service Report',
    )

    styles = getSampleStyleSheet()
    org = ParagraphStyle('org', parent=styles['Heading2'], alignment=TA_CENTER,
                         fontSize=13, leading=16, textColor=colors.HexColor('#0f172a'),
                         spaceAfter=2)
    org_sub = ParagraphStyle('org_sub', parent=styles['Normal'], alignment=TA_CENTER,
                             fontSize=8, leading=11, textColor=colors.HexColor('#64748b'),
                             spaceAfter=14)
    h1 = ParagraphStyle('h1', parent=styles['Heading1'], alignment=TA_CENTER,
                        fontSize=18, leading=22, textColor=colors.HexColor('#0f172a'),
                        spaceAfter=4)
    sub = ParagraphStyle('sub', parent=styles['Normal'], alignment=TA_CENTER,
                         fontSize=10, leading=14, textColor=colors.HexColor('#64748b'),
                         spaceAfter=16)
    h2 = ParagraphStyle('h2', parent=styles['Heading2'], fontSize=12, leading=15,
                        textColor=colors.HexColor('#1e3a5f'), spaceBefore=14, spaceAfter=6)
    body = ParagraphStyle('body', parent=styles['Normal'], fontSize=10, leading=14,
                          textColor=colors.HexColor('#1f2937'), spaceAfter=4)
    cell = ParagraphStyle('cell', parent=styles['Normal'], fontSize=8.5, leading=11,
                          textColor=colors.HexColor('#1f2937'))
    cell_b = ParagraphStyle('cell_b', parent=cell, textColor=colors.white, fontSize=9)

    story = []

    # ── Header ─────────────────────────────────────────────────────────────
    story.append(Paragraph(b['org_name'].upper(), org))
    bits = [x for x in [b['org_addr'], b['org_phone'], b['org_email']] if x]
    if bits:
        story.append(Paragraph(' · '.join(bits), org_sub))

    story.append(Paragraph('MONTHLY SERVICE REPORT', h1))
    story.append(Paragraph(report.period_label, sub))

    # Meta line: who / period / prepared
    customer = report.contract.counterparty_name if report.contract_id else report.customer_name
    scope_label = report.get_scope_display()
    prepared_by = report.generated_by.full_name if report.generated_by else '—'
    meta_rows = [
        [Paragraph('<b>Customer</b>', cell), Paragraph(customer or '—', cell),
         Paragraph('<b>Reporting period</b>', cell),
         Paragraph(f'{report.period_start:%d %b %Y} – {report.period_end:%d %b %Y}', cell)],
        [Paragraph('<b>Scope</b>', cell), Paragraph(scope_label, cell),
         Paragraph('<b>Prepared by</b>', cell), Paragraph(prepared_by, cell)],
    ]
    if report.contract_id:
        meta_rows.append([
            Paragraph('<b>Contract</b>', cell), Paragraph(report.contract.title, cell),
            Paragraph('<b>Reference</b>', cell), Paragraph(report.contract.reference or '—', cell),
        ])
    mt = Table(meta_rows, colWidths=[inner_w*0.16, inner_w*0.34, inner_w*0.18, inner_w*0.32])
    mt.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor('#f8fafc')),
        ('BOX', (0, 0), (-1, -1), 0.5, colors.HexColor('#e2e8f0')),
        ('INNERGRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#e2e8f0')),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING', (0, 0), (-1, -1), 5),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
        ('LEFTPADDING', (0, 0), (-1, -1), 8),
    ]))
    story.append(mt)

    # ── 1. Executive summary ────────────────────────────────────────────────
    story.append(Paragraph('1. Executive Summary', h2))
    story.append(Paragraph(
        f'This report covers all on-site service and maintenance activities '
        f'carried out during {report.period_label}. '
        f'A total of {report.total_activities} activit'
        f'{"y was" if report.total_activities == 1 else "ies were"} logged '
        f'across {report.total_visits} site visit'
        f'{"" if report.total_visits == 1 else "s"}.',
        body,
    ))

    status_counts = Counter()
    device_visits = 0
    for v in visits:
        for a in v.activities.all():
            status_counts[a.status] += 1
            if a.device:
                device_visits += 1

    summary_rows = [
        [Paragraph('<b>Metric</b>', cell_b), Paragraph('<b>Count</b>', cell_b)],
        [Paragraph('Total site visits', cell), Paragraph(str(report.total_visits), cell)],
        [Paragraph('Total activities logged', cell), Paragraph(str(report.total_activities), cell)],
        [Paragraph('Activities resolved / done', cell),
         Paragraph(str(status_counts.get('done', 0)), cell)],
        [Paragraph('Pending / follow-up', cell),
         Paragraph(str(status_counts.get('pending', 0) + status_counts.get('in_progress', 0)), cell)],
        [Paragraph('Escalated', cell),
         Paragraph(str(status_counts.get('escalated', 0)), cell)],
    ]
    st = Table(summary_rows, colWidths=[inner_w*0.72, inner_w*0.28])
    st.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#1e3a5f')),
        ('BOX', (0, 0), (-1, -1), 0.5, colors.HexColor('#cbd5e1')),
        ('INNERGRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#e2e8f0')),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#f8fafc')]),
        ('ALIGN', (1, 0), (1, -1), 'CENTER'),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING', (0, 0), (-1, -1), 5),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
        ('LEFTPADDING', (0, 0), (-1, -1), 8),
    ]))
    story.append(st)

    # ── 2. Detailed activity log ─────────────────────────────────────────────
    story.append(Paragraph('2. Detailed Activity Log', h2))
    story.append(Paragraph(
        'All recorded on-site interventions for the period, in chronological order.',
        body,
    ))

    log_header = [
        Paragraph('<b>Date</b>', cell_b),
        Paragraph('<b>Device / Asset</b>', cell_b),
        Paragraph('<b>Issue / Task</b>', cell_b),
        Paragraph('<b>Tag / Serial / User</b>', cell_b),
        Paragraph('<b>Status</b>', cell_b),
    ]
    log_rows = [log_header]

    # Flatten all activities across visits, sorted by date
    all_activities = []
    for v in visits:
        for a in v.activities.all():
            all_activities.append(a)
    all_activities.sort(key=lambda a: (a.activity_date, a.position))

    status_display = dict(_status_choices())
    if all_activities:
        for a in all_activities:
            log_rows.append([
                Paragraph(f'{a.activity_date:%d %b %Y}', cell),
                Paragraph(a.device or '—', cell),
                Paragraph(a.task_description or '—', cell),
                Paragraph(a.serial_or_user or '—', cell),
                Paragraph(status_display.get(a.status, a.status), cell),
            ])
    else:
        log_rows.append([Paragraph('No activities were logged for this period.', cell),
                         '', '', '', ''])

    lt = Table(
        log_rows,
        colWidths=[inner_w*0.13, inner_w*0.20, inner_w*0.34, inner_w*0.20, inner_w*0.13],
        repeatRows=1,
    )
    lt.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#1e3a5f')),
        ('BOX', (0, 0), (-1, -1), 0.5, colors.HexColor('#cbd5e1')),
        ('INNERGRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#e2e8f0')),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#f8fafc')]),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('TOPPADDING', (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
        ('LEFTPADDING', (0, 0), (-1, -1), 6),
        ('RIGHTPADDING', (0, 0), (-1, -1), 6),
    ]))
    if all_activities:
        story.append(lt)

    # ── 3. Sign-off ───────────────────────────────────────────────────────────
    story.append(Spacer(1, 0.6 * cm))
    story.append(Paragraph('3. Authorisation', h2))
    story.append(Paragraph(
        'This report has been reviewed and approved for release to the customer. '
        'An electronic signature shall have the same legal force and effect as a '
        'handwritten signature.',
        body,
    ))
    story.append(Spacer(1, 0.4 * cm))

    # Two-column sign-off. The right column reserves space for the CEO
    # signature stamp applied by stamp_signature_onto_pdf (last page).
    sign_rows = [
        [Paragraph('<b>Prepared by</b>', cell), Paragraph('<b>Approved by (CEO)</b>', cell)],
        [Paragraph(f'{prepared_by}<br/><br/>Signature: ______________________<br/>'
                   f'Date: {timezone.localdate():%d/%m/%Y}', cell),
         Paragraph('Signature: <i>[signature here]</i><br/><br/>'
                   'Name: ______________________<br/>Date: ______________________', cell)],
    ]
    sgt = Table(sign_rows, colWidths=[inner_w*0.5, inner_w*0.5])
    sgt.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#f1f5f9')),
        ('BOX', (0, 0), (-1, -1), 0.5, colors.HexColor('#cbd5e1')),
        ('INNERGRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#e2e8f0')),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('TOPPADDING', (0, 0), (-1, -1), 8),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 18),
        ('LEFTPADDING', (0, 0), (-1, -1), 10),
    ]))
    story.append(sgt)

    doc.build(story)
    return buf.getvalue()


def _status_choices():
    from apps.customer_portal.models_onsite_report import VisitActivity
    return VisitActivity.Status.choices


# ─────────────────────────────────────────────────────────────────────────────
# CEO signature flow (reuses contract signing machinery) + delivery
# ─────────────────────────────────────────────────────────────────────────────

def send_report_for_ceo_signature(report, *, ceo_name: str, ceo_email: str, actor=None):
    """
    Wrap the report PDF as a ContractDocument and create a
    ContractSignatureRequest so the existing signing UI/flow handles it.

    Requires the report to belong to a contract (the signing models hang
    off finance.Contract). For CUSTOMER-scope reports, attach to any one of
    the customer's contracts as the signing anchor.
    """
    from apps.finance.models import Contract, ContractDocument, ContractSignatureRequest
    from apps.customer_portal.models_onsite_report import MonthlyServiceReport

    if not report.pdf_file:
        raise OnSiteReportError('Report has no PDF — generate it first.')

    contract = report.contract
    if contract is None:
        contract = (
            Contract.objects
            .filter(counterparty_name__iexact=report.customer_name)
            .order_by('-created_at')
            .first()
        )
    if contract is None:
        raise OnSiteReportError(
            'No contract found to anchor the signature request. '
            'A signing anchor contract is required.'
        )

    # Reuse the PDF hash captured at generation time.
    report.pdf_file.open('rb')
    try:
        raw = report.pdf_file.read()
    finally:
        report.pdf_file.close()

    last = ContractDocument.objects.filter(
        contract=contract, source=ContractDocument.Source.GENERATED,
    ).order_by('-version').first()
    next_version = (last.version + 1) if last else 1

    cdoc = ContractDocument(
        contract=contract,
        source=ContractDocument.Source.GENERATED,
        title=f'{report.title or "Monthly Service Report"}',
        version=next_version,
        generated_by=actor,
    )
    base = (contract.reference or f'contract-{contract.pk}').replace('/', '-')
    cdoc.pdf_file.save(
        f'{base}-monthly-{report.period_start:%Y-%m}.pdf',
        ContentFile(raw), save=False,
    )
    cdoc.save()

    sig_req = ContractSignatureRequest(
        contract=contract,
        document=cdoc,
        signer_name=ceo_name.strip(),
        signer_email=ceo_email.strip(),
        message=f'Please sign the {report.period_label} service report for '
                f'{report.customer_name or contract.counterparty_name}.',
        status=ContractSignatureRequest.Status.DRAFT,
        expires_at=timezone.now() + timezone.timedelta(days=30),
        created_by=actor,
    )
    sig_req.document_hash = report.pdf_hash
    sig_req.save()

    report.signature_request = sig_req
    report.status = MonthlyServiceReport.Status.AWAITING_SIGN
    report.save(update_fields=['signature_request', 'status', 'updated_at'])
    return sig_req


def email_signed_report(report) -> None:
    """
    Email the signed PDF to the contract's PM contacts. Call after the CEO
    has signed (signature_request.status == SIGNED / COMPLETED).
    """
    from django.core.mail import EmailMessage
    from apps.customer_portal.models_onsite_report import MonthlyServiceReport

    sig = report.signature_request
    signed_field = getattr(sig, 'signed_pdf', None) if sig else None
    pdf_field = signed_field or report.pdf_file
    if not pdf_field:
        raise OnSiteReportError('No PDF available to send.')

    emails = _pm_contact_emails(report)
    if not emails:
        raise OnSiteReportError('No active PM contacts to email this report to.')

    org = _branding()['org_name']
    customer = report.contract.counterparty_name if report.contract_id else report.customer_name
    subject = f'{report.period_label} Service Report — {org}'
    body = (
        f'Dear {customer or "Customer"},\n\n'
        f'Please find attached your service report for {report.period_label}, '
        f'covering {report.total_activities} activities across '
        f'{report.total_visits} site visit(s).\n\n'
        f'This report has been reviewed and digitally signed.\n\n'
        f'Thank you for your business.\n{org}'
    )
    msg = EmailMessage(
        subject=subject, body=body,
        from_email=getattr(settings, 'DEFAULT_FROM_EMAIL', None),
        to=emails,
    )
    pdf_field.open('rb')
    try:
        msg.attach(
            f'service-report-{report.period_start:%Y-%m}.pdf',
            pdf_field.read(), 'application/pdf',
        )
    finally:
        pdf_field.close()
    msg.send(fail_silently=False)

    report.status = MonthlyServiceReport.Status.SENT
    report.sent_at = timezone.now()
    report.save(update_fields=['status', 'sent_at', 'updated_at'])


def _pm_contact_emails(report):
    try:
        from apps.customer_portal.models import ContractContact
    except Exception:
        return []
    from apps.finance.models import Contract

    if report.contract_id:
        contracts = [report.contract]
    else:
        contracts = list(
            Contract.objects.filter(counterparty_name__iexact=report.customer_name)
        )
    if not contracts:
        return []
    return list(
        ContractContact.objects
        .filter(contract__in=contracts, is_active=True, receives_pm_notices=True)
        .values_list('email', flat=True)
        .distinct()
    )
