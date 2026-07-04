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
    build_offer_letter_pdf_with_sign_meta(onboarding) -> (bytes, sign_meta)
    save_offer_letter_for_onboarding(onboarding, user, pdf_bytes=None)
        -> (EmployeeDocument, SharedFile|None)
    send_offer_letter_for_signature(onboarding, user, base_url, ...)
        -> (EmployeeDocument, SharedFile, SignatureRequest, Signer)
        Generates the letter, emails it to the employee with a
        "Review & Sign" link, and pre-places the acceptance signature
        fields using the Files app signature flow.
    handle_offer_signature_completed(sig_req)
        Called by apps.files.tasks when the employee has signed — marks
        the onboarding record and attaches the signed PDF.

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

def _make_offer_letter_overlay(onboarding, page_width: float, page_height: float):
    """
    Render the offer letter body as a single PDF page sized to the letterhead.

    Layout: a slightly inset content frame, leaving generous top/bottom margins
    so the letterhead's existing header/footer artwork remains visible.

    Returns (pdf_bytes, sign_meta) where sign_meta describes where the
    "Accepted by Employee" signature/date lines landed, expressed in the
    SignatureField percentage convention used by apps.files
    (x/y are % of page size, y measured FROM THE TOP — see
    views._embed_signatures_in_pdf: fy = h * (1 - (y_pct + height_pct)/100)).
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
    page_num = 1
    if y < bottom + 90:
        c.showPage()
        c.setPageSize((page_width, page_height))
        y = top
        c.setFillColor(ink)
        page_num = 2

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

    # ── Signing-field geometry for the Files app signature flow ────────────
    # Convert the acceptance lines into SignatureField percentage boxes.
    # y_pct is measured from the TOP of the page (screen convention), while
    # ReportLab's ack_y is from the bottom — hence the (page_h - top) flips.
    def _pct_box(x, y_top_pdf, w, h):
        return {
            'x_pct':      round(max(0.0, x / page_width * 100.0), 2),
            'y_pct':      round(max(0.0, (page_height - y_top_pdf) / page_height * 100.0), 2),
            'width_pct':  round(min(100.0, w / page_width * 100.0), 2),
            'height_pct': round(min(100.0, h / page_height * 100.0), 2),
        }

    sig_x = ack_x + 52                    # just after the "Signature:" label
    sig_w = max(120.0, right - sig_x)     # to the right margin
    sign_meta = {
        'page': page_num,
        # Drawn/typed signature box sits on top of the signature line.
        'signature': _pct_box(sig_x, ack_y - 10, sig_w, 36),
        # Date box sits on the "Date: ____" line below it.
        'date':      _pct_box(ack_x + 32, ack_y - 48, 150, 14),
    }

    c.save()
    return buffer.getvalue(), sign_meta


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

def build_offer_letter_pdf_with_sign_meta(onboarding):
    """
    Build the final offer letter PDF and the signing-field geometry.

    Returns (pdf_bytes, sign_meta). sign_meta locates the
    "Accepted by Employee" signature/date lines so the Files-app signature
    flow can drop SignatureField boxes exactly on them.
    """
    letterhead_bytes = get_offer_letterhead_pdf_bytes()
    page_w, page_h = _first_pdf_page_size(letterhead_bytes)
    overlay_bytes, sign_meta = _make_offer_letter_overlay(onboarding, page_w, page_h)
    if letterhead_bytes:
        return _merge_letterhead_and_overlay(letterhead_bytes, overlay_bytes), sign_meta
    return overlay_bytes, sign_meta


def build_offer_letter_pdf(onboarding) -> bytes:
    """
    Build the final offer letter PDF as raw bytes.

    Mirrors build_payslip_pdf in views.py: render the overlay sized to the
    letterhead's page (or A4), then merge with pypdf.
    """
    pdf_bytes, _meta = build_offer_letter_pdf_with_sign_meta(onboarding)
    return pdf_bytes


def offer_letter_filename(onboarding) -> str:
    name = (onboarding.full_name or 'employee').replace(' ', '_')
    code = onboarding.employee_code or 'pending'
    stamp = timezone.now().strftime('%Y%m%d')
    return f'offer_letter_{name}_{code}_{stamp}.pdf'


# ─────────────────────────────────────────────────────────────────────────────
# Persistence — attach to onboarding AND save to Files app
# ─────────────────────────────────────────────────────────────────────────────

def save_offer_letter_for_onboarding(onboarding, user, pdf_bytes=None):
    """
    Generate the offer letter PDF and persist it.

    Returns (employee_document, shared_file_or_none).

    • employee_document — always created (or replaced) on the onboarding
      record so it appears in the Documents table on the detail page.
    • shared_file — created in the Files app under a 'HR / Offer Letters'
      folder. Owned by the new staff_user when it exists, otherwise by
      ``user`` (the CEO/HR person triggering generation).

    ``pdf_bytes`` lets callers that already rendered the PDF (the signing
    flow below) reuse it instead of rendering twice.
    """
    from apps.hr.models import EmployeeDocument

    if pdf_bytes is None:
        pdf_bytes = build_offer_letter_pdf(onboarding)
    filename = offer_letter_filename(onboarding)
    title = f'Offer Letter — {onboarding.employee_code or onboarding.full_name}'

    # ── 1) EmployeeDocument (replace any prior offer letter for this onboarding) ──
    # Note: a signed copy (title ending in "(Signed)") is preserved —
    # regenerating the unsigned letter must never destroy a signed record.
    EmployeeDocument.objects.filter(
        onboarding=onboarding,
        document_type=EmployeeDocument.DocumentType.CONTRACT,
        title__startswith='Offer Letter',
    ).exclude(title__endswith='(Signed)').delete()

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


# ─────────────────────────────────────────────────────────────────────────────
# Send-for-signing — Files app signature flow integration
# ─────────────────────────────────────────────────────────────────────────────

def send_offer_letter_for_signature(onboarding, user, base_url,
                                    expires_days=14, extra_message=''):
    """
    Generate the offer letter, persist it, create a Files-app
    SignatureRequest for the employee, and email them the offer with a
    one-click "Review & Sign" link (token-based, no login required).

    The employee's acceptance signature/date fields are pre-placed exactly
    on the "Accepted by Employee" lines of the letter.

    Returns (employee_doc, shared_file, sig_req, signer).
    Raises ValueError with a user-friendly message on any hard failure.
    """
    from apps.files.models import (
        SignatureRequest,
        SignatureRequestSigner,
        SignatureRequestDocument,
        SignatureAuditEvent,
        SignatureField,
    )

    # ── 0) Resolve the recipient ────────────────────────────────────────────
    to_email = (onboarding.personal_email or getattr(onboarding, 'official_email', '') or '').strip()
    if not to_email:
        raise ValueError(
            'This employee has no personal or official email on record — '
            'add an email address before sending the offer for signing.'
        )
    to_name = onboarding.full_name or to_email

    # ── 1) Build + persist the PDF (single render) ─────────────────────────
    pdf_bytes, sign_meta = build_offer_letter_pdf_with_sign_meta(onboarding)
    employee_doc, shared_file = save_offer_letter_for_onboarding(
        onboarding, user, pdf_bytes=pdf_bytes
    )
    if shared_file is None:
        raise ValueError(
            'The offer letter was generated, but it could not be saved to '
            'the Files app — the signature flow needs a Files-app document.'
        )

    # ── 2) Cancel any previous still-open offer-letter request ─────────────
    # Re-sending the offer should not leave two live signing links around.
    try:
        stale = SignatureRequest.objects.filter(
            metadata__hr__kind='offer_letter',
            metadata__hr__onboarding_id=str(onboarding.pk),
            status__in=['sent', 'partial'],
        )
        for old in stale:
            old.status = SignatureRequest.Status.CANCELLED
            old.save(update_fields=['status', 'updated_at'])
            old.signers.filter(status__in=['pending', 'viewed']).update(status='cancelled')
            SignatureAuditEvent.objects.create(
                request=old, event='cancelled',
                notes='Superseded by a newly issued offer letter.',
            )
    except Exception:
        logger.exception('Could not cancel stale offer signature requests '
                         'for onboarding %s', onboarding.pk)

    # ── 3) Create the SignatureRequest ──────────────────────────────────────
    title = f'Offer Letter — {onboarding.full_name or onboarding.employee_code}'
    message = (
        'Please review your letter of offer carefully. If you are satisfied '
        'with the terms of this offer, sign in the "Accepted by Employee" '
        'section to confirm your acceptance.'
    )
    if extra_message:
        message = f'{message}\n\n{extra_message}'

    sig_req = SignatureRequest.objects.create(
        title=title,
        message=message,
        document=shared_file,
        created_by=user,
        status=SignatureRequest.Status.SENT,
        metadata={'hr': {
            'kind': 'offer_letter',
            'onboarding_id': str(onboarding.pk),
            'employee_code': onboarding.employee_code or '',
        }},
    )
    if expires_days:
        from datetime import timedelta
        sig_req.expires_at = timezone.now() + timedelta(days=int(expires_days))
        sig_req.save(update_fields=['expires_at'])

    try:
        SignatureRequestDocument.objects.create(
            request=sig_req, document=shared_file, order=1, is_primary=True,
        )
    except Exception:
        pass  # legacy single-FK path still works

    # ── 4) Signer = the employee ────────────────────────────────────────────
    signer_user = onboarding.staff_user
    if signer_user is None:
        try:
            from apps.core.models import User as CoreUser
            signer_user = CoreUser.objects.filter(email__iexact=to_email).first()
        except Exception:
            signer_user = None

    signer = SignatureRequestSigner.objects.create(
        request=sig_req,
        user=signer_user,
        email=to_email,
        name=to_name,
        order=1,
        invited_at=timezone.now(),
    )

    # ── 5) Pre-place signature + date fields on the acceptance lines ────────
    try:
        page = int(sign_meta.get('page', 1))
        s = sign_meta.get('signature') or {}
        d = sign_meta.get('date') or {}
        if s:
            SignatureField.objects.create(
                request=sig_req, signer=signer,
                field_type=SignatureField.FieldType.SIGNATURE,
                page=page, label='Accepted by Employee', required=True,
                x_pct=s['x_pct'], y_pct=s['y_pct'],
                width_pct=s['width_pct'], height_pct=s['height_pct'],
            )
        if d:
            SignatureField.objects.create(
                request=sig_req, signer=signer,
                field_type=SignatureField.FieldType.DATE,
                page=page, label='Acceptance Date', required=True,
                x_pct=d['x_pct'], y_pct=d['y_pct'],
                width_pct=d['width_pct'], height_pct=d['height_pct'],
            )
    except Exception:
        # Without fields the sign page still works — the signer just signs
        # without pre-placed boxes. Never block the send over placement.
        logger.exception('Could not pre-place offer signature fields for %s', sig_req.pk)

    # ── 6) Audit trail ───────────────────────────────────────────────────────
    try:
        SignatureAuditEvent.objects.create(
            request=sig_req, event='created',
            notes=f'Offer letter issued by {user.full_name} (HR onboarding '
                  f'{onboarding.employee_code or onboarding.pk}).',
        )
        SignatureAuditEvent.objects.create(
            request=sig_req, event='sent',
            signer_email=to_email, signer_name=to_name,
            notes='Offer letter emailed to employee with signing link.',
        )
        sig_req.rebuild_audit_hash()
    except Exception:
        logger.exception('Could not write audit events for %s', sig_req.pk)

    # ── 7) Email the employee: offer PDF attached + Review & Sign link ─────
    # Sent in a daemon thread (same pattern as apps.files) so the HTTP
    # response returns immediately.
    def _send():
        _send_offer_signing_email(onboarding, sig_req, signer, base_url, pdf_bytes)

    try:
        from apps.files.views import _spawn
        _spawn(_send)
    except Exception:
        try:
            _send()
        except Exception:
            logger.exception('Could not send offer signing email for %s', sig_req.pk)

    return employee_doc, shared_file, sig_req, signer


def _send_offer_signing_email(onboarding, sig_req, signer, base_url, pdf_bytes):
    """HTML offer email: copy of the letter attached + a Review & Sign CTA."""
    from django.conf import settings
    from django.core.mail import EmailMessage

    org_name = getattr(settings, 'ORGANISATION_NAME',
                       getattr(settings, 'OFFICE_NAME', 'EasyOffice'))
    from_email = getattr(settings, 'DEFAULT_FROM_EMAIL',
                         f'noreply@{org_name.lower().replace(" ", "")}.org')
    sign_url = base_url.rstrip('/') + signer.signing_url
    job_title = onboarding.job_title_display or onboarding.job_title or 'your new role'
    expires = (sig_req.expires_at.strftime('%d %B %Y') if sig_req.expires_at else None)
    first_name = onboarding.first_name or onboarding.full_name or 'Colleague'

    html = f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<style>
  body{{margin:0;padding:0;background:#f1f5f9;font-family:'Segoe UI',Arial,sans-serif;}}
  .w{{max-width:620px;margin:28px auto;background:#fff;border-radius:16px;overflow:hidden;box-shadow:0 4px 24px rgba(0,0,0,.08);}}
  .hdr{{background:linear-gradient(135deg,#0f3d73,#145aa0 58%,#0d9488);padding:36px 40px;text-align:center;}}
  .hdr h1{{margin:0 0 4px;font-size:22px;color:#fff;font-weight:800;}}
  .hdr p{{margin:0;font-size:13px;color:rgba(255,255,255,.8);}}
  .badge{{font-size:40px;display:block;margin-bottom:12px;}}
  .body{{padding:32px 40px;}}
  .greeting{{font-size:15.5px;color:#1e293b;line-height:1.75;margin-bottom:18px;}}
  .doc-box{{background:#f8fafc;border:1.5px solid #e2e8f0;border-radius:12px;padding:16px 20px;margin:18px 0;display:flex;align-items:center;gap:14px;}}
  .doc-name{{font-weight:700;font-size:15px;color:#1e293b;}}
  .doc-from{{font-size:13px;color:#64748b;margin-top:3px;}}
  .accept-box{{background:#ecfdf5;border:1px solid #a7f3d0;border-radius:12px;padding:14px 18px;font-size:14px;color:#065f46;line-height:1.7;margin:16px 0;}}
  .cta{{text-align:center;margin:28px 0;}}
  .btn{{display:inline-block;background:linear-gradient(135deg,#0f3d73,#0d9488);color:#fff!important;padding:14px 38px;border-radius:12px;text-decoration:none;font-weight:800;font-size:16px;box-shadow:0 4px 16px rgba(15,61,115,.3);}}
  .url-note{{font-size:12px;color:#94a3b8;text-align:center;margin-top:8px;word-break:break-all;}}
  .expire{{background:#fff7ed;border:1px solid #fed7aa;border-radius:8px;padding:10px 14px;font-size:13px;color:#9a3412;margin-top:14px;}}
  .warn{{background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;padding:10px 14px;font-size:12.5px;color:#475569;margin-top:14px;line-height:1.6;}}
  .footer{{background:#f8fafc;padding:20px 40px;text-align:center;border-top:1px solid #e2e8f0;}}
  .footer p{{margin:0;font-size:12px;color:#94a3b8;line-height:1.8;}}
</style></head>
<body>
<div class="w">
  <div class="hdr">
    <span class="badge">🎉</span>
    <h1>Your Letter of Offer</h1>
    <p>{org_name} — Human Resources</p>
  </div>
  <div class="body">
    <p class="greeting">Dear <strong>{first_name}</strong>,<br><br>
    Congratulations! We are pleased to offer you the position of
    <strong>{job_title}</strong> at {org_name}. A copy of your official
    letter of offer is attached to this email for your records.</p>

    <div class="doc-box">
      <div style="font-size:2rem">📄</div>
      <div>
        <div class="doc-name">{sig_req.title}</div>
        <div class="doc-from">Attached as PDF · {org_name}</div>
      </div>
    </div>

    <div class="accept-box">
      ✅ <strong>If you are satisfied with the terms of this offer</strong>,
      please click the button below to review the letter and digitally sign
      in the <em>“Accepted by Employee”</em> section. Signing confirms your
      acceptance of the offer — no printing or scanning is required.
    </div>

    <div class="cta">
      <a href="{sign_url}" class="btn">🖊 Review &amp; Sign Offer Letter</a>
      <div class="url-note">Or copy this link: {sign_url}</div>
    </div>

    {'<div class="expire">⏰ This offer link expires on <strong>' + expires + '</strong>. Please sign before then.</div>' if expires else ''}

    <div class="warn">🔒 This signing link is unique to you and should not be
    shared. No login is required. If you have any questions about the terms
    of the offer, please contact HR before signing.</div>
  </div>
  <div class="footer">
    <p><strong>{org_name}</strong><br>This is an automated message from the
    HR onboarding system. Please do not reply to this email.</p>
  </div>
</div>
</body></html>"""

    msg = EmailMessage(
        subject=f'Offer of Employment — Action Required | {org_name}',
        body=html,
        from_email=from_email,
        to=[signer.email],
    )
    msg.content_subtype = 'html'
    try:
        msg.attach(offer_letter_filename(onboarding), pdf_bytes, 'application/pdf')
    except Exception:
        logger.exception('Could not attach offer PDF to signing email for %s', sig_req.pk)
    msg.send(fail_silently=False)


def handle_offer_signature_completed(sig_req):
    """
    Called from apps.files.tasks.finalise_signature_after_sign once every
    signer on an offer-letter request has signed.

    • Marks the onboarding record's offer letter as signed.
    • Replaces the offer letter under "Documents, Work History & References"
      with the stamped/signed PDF — the original unsigned EmployeeDocument
      row is overwritten in place and retitled "… (Signed)".
    """
    from apps.hr.models import EmployeeOnboarding, EmployeeDocument

    hr_meta = (sig_req.metadata or {}).get('hr') or {}
    onboarding_id = hr_meta.get('onboarding_id')
    if hr_meta.get('kind') != 'offer_letter' or not onboarding_id:
        return

    try:
        onboarding = EmployeeOnboarding.objects.get(pk=onboarding_id)
    except EmployeeOnboarding.DoesNotExist:
        logger.warning('Offer signature completed but onboarding %s is gone',
                       onboarding_id)
        return

    signer = sig_req.signers.order_by('order').first()

    # ── Mark signed on the onboarding record ────────────────────────────────
    try:
        if not onboarding.offer_letter_signed:
            onboarding.offer_letter_signed = True
            onboarding.offer_letter_signed_at = (
                signer.signed_at if signer and signer.signed_at else timezone.now()
            )
            # Recorded as confirmed by the staff account when the employee
            # already has one, otherwise by whoever issued the request.
            onboarding.offer_letter_signed_by = (
                onboarding.staff_user or sig_req.created_by
            )
            onboarding.save(update_fields=[
                'offer_letter_signed',
                'offer_letter_signed_at',
                'offer_letter_signed_by',
            ])
    except Exception:
        logger.exception('Could not mark offer letter signed for onboarding %s',
                         onboarding_id)

    # ── Replace the offer letter in "Documents, Work History & References" ──
    # sig_req.document now points at the stamped/signed copy (the embedder
    # swaps it after completion). Instead of adding a second row, we overwrite
    # the ORIGINAL "Offer Letter — …" EmployeeDocument in place so the
    # detail-page table shows one entry: the signed version.
    try:
        signed_doc = sig_req.document
        if signed_doc and getattr(signed_doc, 'file', None):
            signed_bytes = _read_pdf_bytes_from_field(signed_doc.file)
            if signed_bytes:
                signed_title = (
                    f'Offer Letter — '
                    f'{onboarding.employee_code or onboarding.full_name} (Signed)'
                )

                offer_docs = EmployeeDocument.objects.filter(
                    onboarding=onboarding,
                    document_type=EmployeeDocument.DocumentType.CONTRACT,
                    title__startswith='Offer Letter',
                ).order_by('-uploaded_at')

                target = offer_docs.first()

                # Drop duplicates and any legacy 'Signed Offer Letter' rows so
                # exactly one offer-letter document remains after this runs.
                offer_docs.exclude(pk=getattr(target, 'pk', None)).delete()
                EmployeeDocument.objects.filter(
                    onboarding=onboarding,
                    document_type=EmployeeDocument.DocumentType.CONTRACT,
                    title__startswith='Signed Offer Letter',
                ).delete()

                if target is None:
                    # Original was removed somehow — recreate the row.
                    target = EmployeeDocument.objects.create(
                        onboarding=onboarding,
                        document_type=EmployeeDocument.DocumentType.CONTRACT,
                        title=signed_title,
                        uploaded_by=sig_req.created_by,
                    )
                else:
                    target.title = signed_title
                    target.save(update_fields=['title'])
                    # Remove the unsigned PDF blob before writing the new one.
                    try:
                        target.file.delete(save=False)
                    except Exception:
                        pass

                target.file.save(
                    f'signed_{offer_letter_filename(onboarding)}',
                    ContentFile(signed_bytes),
                    save=True,
                )
    except Exception:
        logger.exception('Could not replace offer letter with signed PDF for '
                         'onboarding %s', onboarding_id)