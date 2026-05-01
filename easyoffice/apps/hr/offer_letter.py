"""
apps/hr/offer_letter.py

Generates the official Offer Letter PDF for an EmployeeOnboarding record.

Strategy
────────
1. Look up an active letterhead template (priority: apps.letterhead.LetterheadTemplate
   active flag, then the existing payslip letterhead helper, then plain).
2. Render the offer letter body as a ReportLab overlay sized to the letterhead
   page. If no letterhead is found, render an A4 page with a plain header.
3. Merge overlay onto letterhead (when present) using the same pattern as
   build_payslip_pdf — pypdf/PyPDF2 page.merge_page.
4. Persist the generated PDF in TWO places:
     • As an EmployeeDocument attached to the onboarding record
       (document_type = CONTRACT, title = 'Offer Letter — <code>')
     • As a SharedFile in the Files app, owned by the new staff_user when
       available, otherwise by the CEO/HR user who triggered generation.

Public entry points
───────────────────
    build_offer_letter_pdf(onboarding) -> bytes
    save_offer_letter_for_onboarding(onboarding, user) -> (EmployeeDocument, SharedFile|None)

The module is deliberately import-light: heavy imports (ReportLab, pypdf,
SharedFile, LetterheadTemplate) are deferred to call sites so a missing
optional dependency doesn't break the rest of the HR app at import time.
"""
from __future__ import annotations

import hashlib
import logging
from datetime import date
from decimal import Decimal
from io import BytesIO

from django.core.files.base import ContentFile
from django.db.models import Q
from django.utils import timezone

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Letterhead resolution
# ─────────────────────────────────────────────────────────────────────────────

def _read_pdf_bytes_from_field(file_field) -> bytes:
    """Safely read bytes from any FileField/ImageField-like object."""
    if not file_field:
        return b''
    try:
        file_field.open('rb')
        try:
            return file_field.read()
        finally:
            file_field.close()
    except Exception:
        return b''


def _read_shared_pdf_bytes(shared_file) -> bytes:
    """Read PDF bytes from a SharedFile — same shape as in views.py."""
    if not shared_file or not getattr(shared_file, 'file', None):
        return b''
    return _read_pdf_bytes_from_field(shared_file.file)


def get_offer_letterhead_pdf_bytes() -> bytes:
    """
    Resolve a letterhead PDF as raw bytes.

    Order:
      1. apps.letterhead.LetterheadTemplate.get_active() — render its body via
         the LetterheadPdfExportView helper (best-quality, design-faithful).
      2. The same letterhead used by payslips (a SharedFile PDF).
      3. None (b'') — caller will draw the offer letter on a plain A4 page.
    """
    # ── 1) Active LetterheadTemplate, exported as PDF ───────────────────────
    try:
        from apps.letterhead.models import LetterheadTemplate
        from apps.letterhead.docx_export import render_letterhead_pdf_bytes  # type: ignore
        active = LetterheadTemplate.get_active()
        if active is not None:
            try:
                return render_letterhead_pdf_bytes(active) or b''
            except Exception:
                # Helper may not exist with that exact name — fall through.
                pass
    except Exception:
        pass

    # ── 1b) Same template, but export via the view's internal builder ──────
    # If the letterhead app exposes an internal "build pdf" function under a
    # different name we don't know, fall through silently.

    # ── 2) Reuse the payslip letterhead resolver (returns a SharedFile) ────
    try:
        from apps.hr.views import get_default_payslip_letterhead  # local import
        shared = get_default_payslip_letterhead()
        if shared is not None:
            return _read_shared_pdf_bytes(shared)
    except Exception:
        pass

    return b''


def _first_pdf_page_size(pdf_bytes: bytes):
    """Return (width, height) in points of the first page; A4 default."""
    from reportlab.lib.pagesizes import A4
    if not pdf_bytes:
        return A4
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


def _merge_letterhead_and_overlay(letterhead_bytes: bytes, overlay_bytes: bytes) -> bytes:
    """Merge the offer-letter overlay over the first letterhead page."""
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
        # If the overlay produced more than one page, append the rest plainly
        # so a long offer letter doesn't get truncated.
        for extra_page in overlay_reader.pages[1:]:
            writer.add_page(extra_page)
        output = BytesIO()
        writer.write(output)
        return output.getvalue()
    except Exception:
        return overlay_bytes


# ─────────────────────────────────────────────────────────────────────────────
# Currency / formatting helpers
# ─────────────────────────────────────────────────────────────────────────────

def _money_fmt(value) -> str:
    try:
        return f'{Decimal(str(value or 0)):,.2f}'
    except Exception:
        return '0.00'


def _allowance_total(onboarding) -> Decimal:
    total = Decimal('0')
    for v in (onboarding.allowances or {}).values():
        try:
            total += Decimal(str(v))
        except Exception:
            continue
    return total


def _date_fmt(value, fallback='To be confirmed') -> str:
    if not value:
        return fallback
    try:
        return value.strftime('%d %B %Y')
    except Exception:
        return str(value)


def _company_name() -> str:
    """Best-effort company name lookup for the letter heading/signature."""
    try:
        from apps.letterhead.models import LetterheadTemplate
        active = LetterheadTemplate.get_active()
        if active and active.company_name:
            return active.company_name
    except Exception:
        pass
    return 'EasyOffice'


# ─────────────────────────────────────────────────────────────────────────────
# Overlay builder — the actual offer letter content
# ─────────────────────────────────────────────────────────────────────────────

def _make_offer_letter_overlay(onboarding, page_width: float, page_height: float) -> bytes:
    """
    Render the offer letter body as a single PDF page sized to the letterhead.

    Layout: a slightly inset content frame, leaving generous top/bottom margins
    so the letterhead's existing header/footer artwork remains visible.
    """
    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4
        from reportlab.pdfgen import canvas
        from reportlab.pdfbase.pdfmetrics import stringWidth
    except Exception as exc:
        raise ValueError(
            'ReportLab is required to generate the offer letter PDF. '
            'Install it with: pip install reportlab'
        ) from exc

    buffer = BytesIO()
    c = canvas.Canvas(buffer, pagesize=(page_width, page_height))

    # ── Theme ──────────────────────────────────────────────────────────────
    ink   = colors.HexColor('#0b1f3a')
    brand = colors.HexColor('#0f3d73')
    teal  = colors.HexColor('#0d9488')
    muted = colors.HexColor('#475569')
    soft  = colors.HexColor('#f4f7fb')
    line  = colors.HexColor('#d9e2ec')

    # ── Margins — leave room for letterhead header/footer ──────────────────
    # If we're sitting on a letterhead, we want a healthy top margin so we
    # don't draw over the logo. If we're on a plain page we still want
    # professional margins.
    left   = 56
    right  = page_width - 56
    top    = page_height - 130    # below typical letterhead header area
    bottom = 100                  # above typical letterhead footer area
    width  = right - left

    # ── Title strip ────────────────────────────────────────────────────────
    c.setFillColor(brand)
    c.setFont('Helvetica-Bold', 16)
    c.drawString(left, top, 'LETTER OF OFFER & EMPLOYMENT')
    c.setStrokeColor(teal)
    c.setLineWidth(1.4)
    c.line(left, top - 6, left + 130, top - 6)

    # Reference + date row
    c.setFillColor(muted)
    c.setFont('Helvetica', 9)
    issued = _date_fmt(timezone.now().date(), fallback='')
    ref = f'Ref: {onboarding.employee_code or "—"}'
    c.drawString(left, top - 22, ref)
    c.drawRightString(right, top - 22, f'Issued: {issued}')

    y = top - 50

    # ── Recipient block ────────────────────────────────────────────────────
    c.setFillColor(ink)
    c.setFont('Helvetica-Bold', 10.5)
    c.drawString(left, y, onboarding.full_name or '—')
    y -= 13
    c.setFont('Helvetica', 9.5)
    c.setFillColor(muted)
    if onboarding.address_line:
        c.drawString(left, y, onboarding.address_line)
        y -= 11
    city_region = ' '.join(p for p in [onboarding.city, onboarding.region] if p).strip()
    if city_region:
        c.drawString(left, y, city_region)
        y -= 11
    if onboarding.country:
        c.drawString(left, y, onboarding.country)
        y -= 11
    if onboarding.personal_email:
        c.drawString(left, y, onboarding.personal_email)
        y -= 11

    y -= 10

    # ── Salutation + opening paragraph ─────────────────────────────────────
    c.setFillColor(ink)
    c.setFont('Helvetica', 10.5)
    salutation = f'Dear {onboarding.first_name or onboarding.full_name or "Colleague"},'
    c.drawString(left, y, salutation)
    y -= 18

    company = _company_name()
    job_title = onboarding.job_title_display or onboarding.job_title or 'the role outlined below'
    department = onboarding.department_display or onboarding.department or ''
    intro = (
        f'On behalf of {company}, it is my pleasure to formally offer you the position '
        f'of {job_title}'
        + (f' in the {department} department' if department else '')
        + '. Following the successful conclusion of our recruitment process and the '
        f'approval of the executive office, we are delighted to confirm the following '
        f'terms of your appointment.'
    )
    y = _draw_paragraph(c, intro, left, y, width, font='Helvetica', size=10.2, leading=14, color=ink)
    y -= 10

    # ── Terms table ────────────────────────────────────────────────────────
    c.setFont('Helvetica-Bold', 10.5)
    c.setFillColor(brand)
    c.drawString(left, y, 'Terms of Appointment')
    y -= 4
    c.setStrokeColor(teal); c.setLineWidth(1)
    c.line(left, y, left + 110, y)
    y -= 14

    rows = [
        ('Employee Name',   onboarding.full_name or '—'),
        ('Employee Code',   onboarding.employee_code or '—'),
        ('Position',        job_title),
        ('Department',      department or '—'),
        ('Hire Category',   onboarding.hire_category.name if onboarding.hire_category_id else '—'),
        ('Start Date',      _date_fmt(onboarding.start_date)),
    ]
    if onboarding.end_date:
        rows.append(('End Date', _date_fmt(onboarding.end_date)))
    if onboarding.day_hire_days:
        rows.append(('Engagement Days', f'{onboarding.day_hire_days} day(s)'))
    if onboarding.supervisor_id:
        try:
            sup = onboarding.supervisor
            rows.append(('Reports To', sup.get_full_name() or sup.username))
        except Exception:
            pass
    if onboarding.location_id:
        try:
            rows.append(('Office Location', onboarding.location.name))
        except Exception:
            pass

    label_w = 130
    row_h = 16

    for i, (label, value) in enumerate(rows):
        # Subtle alternating background
        if i % 2 == 0:
            c.setFillColor(soft)
            c.rect(left, y - row_h + 4, width, row_h, stroke=0, fill=1)
        c.setFillColor(muted)
        c.setFont('Helvetica', 9.2)
        c.drawString(left + 6, y - 6, label)
        c.setFillColor(ink)
        c.setFont('Helvetica-Bold', 9.6)
        c.drawString(left + label_w, y - 6, str(value))
        y -= row_h

    # ── Compensation block ─────────────────────────────────────────────────
    y -= 8
    c.setFont('Helvetica-Bold', 10.5)
    c.setFillColor(brand)
    c.drawString(left, y, 'Compensation')
    y -= 4
    c.setStrokeColor(teal)
    c.line(left, y, left + 110, y)
    y -= 16

    salary = onboarding.salary or Decimal('0')
    transport = Decimal(str((onboarding.allowances or {}).get('transport', '0') or '0'))
    other = _allowance_total(onboarding) - transport
    if other < 0:
        other = Decimal('0')
    total = salary + _allowance_total(onboarding)

    comp_rows = [
        ('Monthly Basic Salary',    f'GMD {_money_fmt(salary)}'),
        ('Transport Allowance',     f'GMD {_money_fmt(transport)}'),
        ('Other Allowances',        f'GMD {_money_fmt(other)}'),
        ('Total Monthly Package',   f'GMD {_money_fmt(total)}'),
    ]

    # Compensation table — boxed
    box_top = y + 4
    c.setStrokeColor(line); c.setLineWidth(0.7)
    c.rect(left, y - row_h * len(comp_rows) + 4, width, row_h * len(comp_rows), stroke=1, fill=0)

    for i, (label, value) in enumerate(comp_rows):
        is_last = (i == len(comp_rows) - 1)
        if is_last:
            c.setFillColor(colors.HexColor('#eef6ff'))
            c.rect(left, y - row_h + 4, width, row_h, stroke=0, fill=1)
        c.setFillColor(muted)
        c.setFont('Helvetica' if not is_last else 'Helvetica-Bold', 9.4)
        c.drawString(left + 8, y - 6, label)
        c.setFillColor(ink if not is_last else brand)
        c.setFont('Helvetica-Bold', 9.8)
        c.drawRightString(right - 8, y - 6, str(value))
        y -= row_h

    y -= 14

    # ── Conditions paragraph ───────────────────────────────────────────────
    conditions = (
        'This offer is conditional upon the satisfactory completion of all '
        'pre-employment formalities, including the verification of references, '
        'submission of identification and qualification documents, and your '
        'acceptance of the staff handbook and code of conduct. Your employment '
        'shall be governed by the terms of the formal Employment Contract, '
        'which will be issued separately for your signature.'
    )
    y = _draw_paragraph(c, conditions, left, y, width, font='Helvetica', size=10.0, leading=13.5, color=ink)
    y -= 8

    # If we've run out of room for the closing block, push it onto a second page.
    if y < bottom + 90:
        c.showPage()
        c.setPageSize((page_width, page_height))
        y = top
        c.setFillColor(ink)

    # ── Closing + signature block ──────────────────────────────────────────
    closing = (
        'Please signify your acceptance of this offer by signing and returning the '
        'attached Employment Contract before your start date. We look forward '
        'to welcoming you to the team.'
    )
    y = _draw_paragraph(c, closing, left, y, width, font='Helvetica', size=10.2, leading=14, color=ink)
    y -= 24

    c.setFont('Helvetica', 10.2)
    c.setFillColor(ink)
    c.drawString(left, y, 'Yours sincerely,')
    y -= 48

    # Signature line
    c.setStrokeColor(line); c.setLineWidth(0.8)
    c.line(left, y, left + 220, y)
    y -= 14
    c.setFont('Helvetica-Bold', 10.2)
    c.drawString(left, y, 'Office of the Chief Executive Officer')
    y -= 12
    c.setFont('Helvetica', 9.4)
    c.setFillColor(muted)
    c.drawString(left, y, company)

    # Acknowledgement strip on the right
    ack_x = left + width / 2 + 20
    ack_y = y + 60 + 14  # align top with signature line
    c.setStrokeColor(line)
    c.line(ack_x, ack_y, ack_x + (right - ack_x), ack_y)
    c.setFont('Helvetica-Bold', 9.6)
    c.setFillColor(ink)
    c.drawString(ack_x, ack_y - 14, 'Accepted by Employee')
    c.setFont('Helvetica', 8.8)
    c.setFillColor(muted)
    c.drawString(ack_x, ack_y - 28, f'Name: {onboarding.full_name or ""}')
    c.drawString(ack_x, ack_y - 42, 'Signature: _____________________________')
    c.drawString(ack_x, ack_y - 56, 'Date: _____________________________')

    c.save()
    return buffer.getvalue()


def _draw_paragraph(c, text, x, y, max_width, font='Helvetica', size=10, leading=13, color=None):
    """Word-wrap a paragraph onto a ReportLab canvas; return the next y."""
    from reportlab.pdfbase.pdfmetrics import stringWidth
    if color is not None:
        c.setFillColor(color)
    c.setFont(font, size)

    words = (text or '').split()
    line = ''
    for word in words:
        candidate = f'{line} {word}'.strip()
        if stringWidth(candidate, font, size) <= max_width:
            line = candidate
        else:
            if line:
                c.drawString(x, y, line)
                y -= leading
            line = word
    if line:
        c.drawString(x, y, line)
        y -= leading
    return y


# ─────────────────────────────────────────────────────────────────────────────
# Public PDF builder
# ─────────────────────────────────────────────────────────────────────────────

def build_offer_letter_pdf(onboarding) -> bytes:
    """
    Build the final offer letter PDF as raw bytes.

    Mirrors build_payslip_pdf in views.py: render the overlay sized to the
    letterhead's page (or A4), then merge with pypdf.
    """
    letterhead_bytes = get_offer_letterhead_pdf_bytes()
    page_w, page_h = _first_pdf_page_size(letterhead_bytes)
    overlay_bytes = _make_offer_letter_overlay(onboarding, page_w, page_h)
    if letterhead_bytes:
        return _merge_letterhead_and_overlay(letterhead_bytes, overlay_bytes)
    return overlay_bytes


def offer_letter_filename(onboarding) -> str:
    name = (onboarding.full_name or 'employee').replace(' ', '_')
    code = onboarding.employee_code or 'pending'
    stamp = timezone.now().strftime('%Y%m%d')
    return f'offer_letter_{name}_{code}_{stamp}.pdf'


# ─────────────────────────────────────────────────────────────────────────────
# Persistence — attach to onboarding AND save to Files app
# ─────────────────────────────────────────────────────────────────────────────

def save_offer_letter_for_onboarding(onboarding, user):
    """
    Generate the offer letter PDF and persist it.

    Returns (employee_document, shared_file_or_none).

    • employee_document — always created (or replaced) on the onboarding
      record so it appears in the Documents table on the detail page.
    • shared_file — created in the Files app under a 'HR / Offer Letters'
      folder. Owned by the new staff_user when it exists, otherwise by
      ``user`` (the CEO/HR person triggering generation).
    """
    from apps.hr.models import EmployeeDocument

    pdf_bytes = build_offer_letter_pdf(onboarding)
    filename = offer_letter_filename(onboarding)
    title = f'Offer Letter — {onboarding.employee_code or onboarding.full_name}'

    # ── 1) EmployeeDocument (replace any prior offer letter for this onboarding) ──
    EmployeeDocument.objects.filter(
        onboarding=onboarding,
        document_type=EmployeeDocument.DocumentType.CONTRACT,
        title__startswith='Offer Letter',
    ).delete()

    employee_doc = EmployeeDocument.objects.create(
        onboarding=onboarding,
        document_type=EmployeeDocument.DocumentType.CONTRACT,
        title=title,
        uploaded_by=user,
    )
    # FileField is on the model; save bytes via .save()
    employee_doc.file.save(filename, ContentFile(pdf_bytes), save=True)

    # ── 2) SharedFile in the Files app (best-effort) ───────────────────────
    shared_file = None
    try:
        from apps.files.models import SharedFile, FileFolder
    except Exception:
        return employee_doc, None

    try:
        owner = onboarding.staff_user or user

        # Find or create a tidy "HR / Offer Letters" folder for the owner.
        folder = None
        try:
            hr_root = FileFolder.objects.filter(name='HR', parent__isnull=True).filter(
                Q(owner=owner) | Q(created_by=owner)
            ).first()
        except Exception:
            hr_root = None

        if hr_root is None:
            try:
                # Try the modern field name first (created_by); fall back to owner.
                hr_root = FileFolder.objects.create(name='HR', created_by=owner)
            except Exception:
                try:
                    hr_root = FileFolder.objects.create(name='HR', owner=owner)
                except Exception:
                    hr_root = None

        if hr_root is not None:
            try:
                folder = FileFolder.objects.filter(name='Offer Letters', parent=hr_root).filter(
                    Q(owner=owner) | Q(created_by=owner)
                ).first()
            except Exception:
                folder = None
            if folder is None:
                try:
                    folder = FileFolder.objects.create(name='Offer Letters', parent=hr_root, created_by=owner)
                except Exception:
                    try:
                        folder = FileFolder.objects.create(name='Offer Letters', parent=hr_root, owner=owner)
                    except Exception:
                        folder = None

        # Build SharedFile. Field set varies by version — keep to the safe core.
        shared_file = SharedFile(
            name=filename,
            folder=folder,
            uploaded_by=owner,
            file_size=len(pdf_bytes),
            file_type='application/pdf',
            description=title,
        )
        # Default visibility: private to the staff member.
        try:
            shared_file.visibility = SharedFile.Visibility.PRIVATE
        except Exception:
            pass
        shared_file.file.save(filename, ContentFile(pdf_bytes), save=False)
        shared_file.save()

        # ── Populate the SHA-256 file_hash ─────────────────────────────────
        # Without this, the signature embedder in apps.files.views falls back
        # to the literal string 'N/A' on the tamper-evident footer, because
        # SharedFile.file_hash is a plain CharField with no auto-population.
        # We hash the bytes we already have in memory rather than calling
        # shared_file.compute_hash(), which re-reads the file from storage.
        try:
            shared_file.file_hash = hashlib.sha256(pdf_bytes).hexdigest()
            shared_file.save(update_fields=['file_hash'])
        except Exception:
            logger.exception(
                'Could not store file_hash on offer-letter SharedFile %s',
                getattr(shared_file, 'pk', None),
            )
    except Exception:
        # Files app integration is best-effort — never block the offer letter.
        shared_file = None

    return employee_doc, shared_file