"""
PDF generation for invoices.

Approach:
    1. Open the letterhead PDF with pypdf and read page 1's dimensions.
    2. Build an overlay PDF with reportlab at the same page size containing
       all invoice content (title, number, client, items, totals, etc.).
    3. Merge the overlay onto the letterhead. The letterhead page is repeated
       on every overflow page so header/footer remain visible.
    4. Return the final PDF bytes.

Coordinate system:
    reportlab uses bottom-left origin; PDF.js in the browser uses top-left.
    The frontend passes positions as % of page dimensions (x_pct, y_pct) with
    y_pct measured from the TOP. We convert on this side.
"""
import io
from decimal import Decimal
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import mm
from pypdf import PdfReader, PdfWriter, PageObject, Transformation


# ── Default layout (percentages of page width/height, y from TOP) ────────────
# Each block is positioned by its top-left corner.
DEFAULT_LAYOUT = {
    'title':         {'x_pct': 50.0, 'y_pct': 18.0, 'w_pct': 50.0},  # centered
    'number_block':  {'x_pct': 65.0, 'y_pct': 23.0, 'w_pct': 30.0},
    'bill_to':       {'x_pct':  7.0, 'y_pct': 28.0, 'w_pct': 40.0},
    'ship_to':       {'x_pct': 52.0, 'y_pct': 28.0, 'w_pct': 40.0},
    'items_table':   {'x_pct':  7.0, 'y_pct': 48.0, 'w_pct': 86.0},
    'totals':        {'x_pct': 62.0, 'y_pct': 74.0, 'w_pct': 31.0},
    'bank_details':  {'x_pct':  7.0, 'y_pct': 74.0, 'w_pct': 45.0},
    'notes':         {'x_pct':  7.0, 'y_pct': 88.0, 'w_pct': 86.0},
}

FONT_NORMAL = 'Helvetica'
FONT_BOLD   = 'Helvetica-Bold'
FONT_ITAL   = 'Helvetica-Oblique'


def _fmt_money(value, currency):
    """Format a Decimal as 'GMD 1,234.56'."""
    if value is None:
        return f'{currency} 0.00'
    q = Decimal(value).quantize(Decimal('0.01'))
    # Thousands separator
    whole, frac = str(q).split('.')
    whole_with_sep = '{:,}'.format(int(whole))
    return f'{currency} {whole_with_sep}.{frac}'


def _pct_xy(layout_block, page_w, page_h):
    """Convert a layout block's %-based top-left to reportlab (x, y_from_bottom)."""
    x = (layout_block.get('x_pct', 0) / 100.0) * page_w
    y_from_top = (layout_block.get('y_pct', 0) / 100.0) * page_h
    y = page_h - y_from_top
    w = (layout_block.get('w_pct', 50) / 100.0) * page_w
    return x, y, w


def _merge_layout(custom):
    """Merge user-customised layout on top of defaults."""
    merged = {k: dict(v) for k, v in DEFAULT_LAYOUT.items()}
    if custom:
        for key, val in custom.items():
            if key in merged and isinstance(val, dict):
                merged[key].update(val)
    return merged


# ── Drawing primitives ───────────────────────────────────────────────────────

def _wrap_text(text, font, size, max_width, c):
    """Naive word-wrap using canvas.stringWidth."""
    if not text:
        return []
    lines = []
    for paragraph in str(text).split('\n'):
        words = paragraph.split(' ')
        current = ''
        for w in words:
            candidate = f'{current} {w}'.strip()
            if c.stringWidth(candidate, font, size) <= max_width:
                current = candidate
            else:
                if current:
                    lines.append(current)
                current = w
        if current:
            lines.append(current)
        if not words:
            lines.append('')
    return lines


def _draw_text_block(c, x, y, width, text, font=FONT_NORMAL, size=9,
                     leading=11, color=colors.black, bold_first_line=False):
    """Draw wrapped text starting at (x, y) from top. Returns new y (bottom of block)."""
    if not text:
        return y
    c.setFillColor(color)
    cur_y = y
    first = True
    for line in _wrap_text(text, font, size, width, c):
        f = FONT_BOLD if (first and bold_first_line) else font
        c.setFont(f, size)
        c.drawString(x, cur_y, line)
        cur_y -= leading
        first = False
    return cur_y


def _draw_title(c, invoice, layout, page_w, page_h):
    block = layout['title']
    x, y, w = _pct_xy(block, page_w, page_h)
    c.setFont(FONT_BOLD, 26)
    c.setFillColor(colors.HexColor('#0f172a'))
    c.drawCentredString(x, y, invoice.title_text)
    # thin underline
    c.setStrokeColor(colors.HexColor('#0ea5e9'))
    c.setLineWidth(1.4)
    tw = c.stringWidth(invoice.title_text, FONT_BOLD, 26)
    c.line(x - tw / 2, y - 6, x + tw / 2, y - 6)


def _draw_number_block(c, invoice, layout, page_w, page_h):
    block = layout['number_block']
    x, y, w = _pct_xy(block, page_w, page_h)
    # Key/value pairs
    rows = [
        ('Number',  invoice.number or invoice.preview_number),
        ('Date',    invoice.invoice_date.strftime('%d %b %Y') if invoice.invoice_date else ''),
    ]
    if invoice.due_date:
        rows.append(('Due Date', invoice.due_date.strftime('%d %b %Y')))
    if invoice.po_reference:
        rows.append(('PO Ref', invoice.po_reference))
    if invoice.payment_terms:
        rows.append(('Terms', invoice.payment_terms))

    cur_y = y
    label_w = 60
    for label, value in rows:
        c.setFont(FONT_BOLD, 9)
        c.setFillColor(colors.HexColor('#64748b'))
        c.drawString(x, cur_y, f'{label}:')
        c.setFont(FONT_NORMAL, 9)
        c.setFillColor(colors.HexColor('#0f172a'))
        c.drawString(x + label_w, cur_y, str(value))
        cur_y -= 13


def _draw_address_block(c, x_pct_block, page_w, page_h, heading, body):
    x, y, w = _pct_xy(x_pct_block, page_w, page_h)
    c.setFont(FONT_BOLD, 8)
    c.setFillColor(colors.HexColor('#0ea5e9'))
    c.drawString(x, y, heading.upper())
    cur_y = y - 14
    return _draw_text_block(c, x, cur_y, w, body, font=FONT_NORMAL, size=9,
                            leading=12, color=colors.HexColor('#0f172a'))


def _draw_items_table(c, invoice, layout, page_w, page_h, items, start_y=None):
    """
    Draw the items table. Returns (last_y, overflow_items).
    `overflow_items` is the slice that couldn't fit on this page.
    """
    block = layout['items_table']
    x, y, w = _pct_xy(block, page_w, page_h)
    if start_y is not None:
        y = start_y

    # Bottom limit — leave room for totals/bank/notes on page 1, or footer on overflow pages
    min_y = 120  # pts from bottom

    # Column setup (proportional)
    col_desc_w = w * 0.52
    col_qty_w  = w * 0.12
    col_price_w = w * 0.18
    col_total_w = w * 0.18
    col_desc_x  = x
    col_qty_x   = x + col_desc_w
    col_price_x = col_qty_x + col_qty_w
    col_total_x = col_price_x + col_price_w

    # Header
    c.setFillColor(colors.HexColor('#0ea5e9'))
    c.rect(x, y - 20, w, 20, fill=1, stroke=0)
    c.setFillColor(colors.white)
    c.setFont(FONT_BOLD, 9)
    c.drawString(col_desc_x + 6,  y - 14, 'Description')
    c.drawRightString(col_qty_x + col_qty_w - 6, y - 14, 'Qty')
    c.drawRightString(col_price_x + col_price_w - 6, y - 14, 'Unit Price')
    c.drawRightString(col_total_x + col_total_w - 6, y - 14, 'Total')

    cur_y = y - 20 - 12
    c.setFillColor(colors.HexColor('#0f172a'))
    c.setFont(FONT_NORMAL, 9)

    row_height = 18
    zebra = False
    overflow = []

    for idx, item in enumerate(items):
        # Wrap description
        desc_lines = _wrap_text(item.description, FONT_NORMAL, 9, col_desc_w - 12, c)
        if not desc_lines:
            desc_lines = ['']
        needed = max(row_height, 12 * len(desc_lines) + 6)

        if cur_y - needed < min_y:
            overflow = items[idx:]
            break

        # zebra stripe
        if zebra:
            c.setFillColor(colors.HexColor('#f1f5f9'))
            c.rect(x, cur_y - needed + 2, w, needed, fill=1, stroke=0)
        zebra = not zebra

        c.setFillColor(colors.HexColor('#0f172a'))
        c.setFont(FONT_NORMAL, 9)
        line_y = cur_y - 10
        for dl in desc_lines:
            c.drawString(col_desc_x + 6, line_y, dl)
            line_y -= 12

        qty_str   = f'{item.quantity:.2f}'.rstrip('0').rstrip('.')
        price_str = f'{item.unit_price:,.2f}'
        total_str = f'{item.line_total:,.2f}'

        c.drawRightString(col_qty_x + col_qty_w - 6, cur_y - 10, qty_str)
        c.drawRightString(col_price_x + col_price_w - 6, cur_y - 10, price_str)
        c.setFont(FONT_BOLD, 9)
        c.drawRightString(col_total_x + col_total_w - 6, cur_y - 10, total_str)
        c.setFont(FONT_NORMAL, 9)

        cur_y -= needed

    # bottom border
    c.setStrokeColor(colors.HexColor('#cbd5e1'))
    c.setLineWidth(0.5)
    c.line(x, cur_y + 2, x + w, cur_y + 2)

    return cur_y, overflow


def _draw_totals(c, invoice, layout, page_w, page_h, override_y=None):
    block = layout['totals']
    x, y, w = _pct_xy(block, page_w, page_h)
    if override_y is not None:
        y = override_y
    c.setFont(FONT_NORMAL, 10)
    cur_y = y

    def row(label, value, bold=False):
        nonlocal cur_y
        c.setFont(FONT_BOLD if bold else FONT_NORMAL, 10)
        c.setFillColor(colors.HexColor('#0f172a'))
        c.drawString(x, cur_y, label)
        c.drawRightString(x + w, cur_y, value)
        cur_y -= 14

    row('Subtotal', _fmt_money(invoice.subtotal, invoice.currency))
    if invoice.tax_rate > 0:
        row(f'Tax ({invoice.tax_rate}%)', _fmt_money(invoice.tax_amount, invoice.currency))
    if invoice.discount_amount > 0:
        row('Discount', f'-{_fmt_money(invoice.discount_amount, invoice.currency)}')

    # separator
    c.setStrokeColor(colors.HexColor('#0ea5e9'))
    c.setLineWidth(1.2)
    c.line(x, cur_y + 4, x + w, cur_y + 4)
    cur_y -= 6

    # grand total
    c.setFillColor(colors.HexColor('#0ea5e9'))
    c.rect(x, cur_y - 18, w, 22, fill=1, stroke=0)
    c.setFillColor(colors.white)
    c.setFont(FONT_BOLD, 11)
    c.drawString(x + 6, cur_y - 10, 'TOTAL')
    c.drawRightString(x + w - 6, cur_y - 10, _fmt_money(invoice.total, invoice.currency))


def _draw_bank_details(c, invoice, layout, page_w, page_h, override_y=None):
    if not invoice.bank_details:
        return
    block = layout['bank_details']
    x, y, w = _pct_xy(block, page_w, page_h)
    if override_y is not None:
        y = override_y
    c.setFont(FONT_BOLD, 8)
    c.setFillColor(colors.HexColor('#0ea5e9'))
    c.drawString(x, y, 'PAYMENT DETAILS')
    cur_y = y - 12
    _draw_text_block(c, x, cur_y, w, invoice.bank_details, font=FONT_NORMAL, size=8.5,
                     leading=11, color=colors.HexColor('#334155'))


def _draw_notes(c, invoice, layout, page_w, page_h, override_y=None):
    if not invoice.notes:
        return
    block = layout['notes']
    x, y, w = _pct_xy(block, page_w, page_h)
    if override_y is not None:
        y = override_y
    c.setFont(FONT_BOLD, 8)
    c.setFillColor(colors.HexColor('#0ea5e9'))
    c.drawString(x, y, 'NOTES')
    cur_y = y - 12
    _draw_text_block(c, x, cur_y, w, invoice.notes, font=FONT_ITAL, size=8.5,
                     leading=11, color=colors.HexColor('#475569'))


def _draw_page_footer(c, invoice, page_num, total_pages, page_w):
    """Small footer with page X of Y + invoice number."""
    c.setFont(FONT_NORMAL, 7.5)
    c.setFillColor(colors.HexColor('#94a3b8'))
    footer_num = invoice.number or invoice.preview_number
    c.drawString(30, 20, f'{footer_num}')
    c.drawRightString(page_w - 30, 20, f'Page {page_num} of {total_pages}')


# ── Main entry ───────────────────────────────────────────────────────────────

def build_invoice_pdf(invoice):
    """
    Build the final invoice PDF with the letterhead repeated on every page.
    Returns bytes of the merged PDF.
    """
    # Load letterhead
    invoice.letterhead.file.open('rb')
    try:
        letterhead_bytes = invoice.letterhead.file.read()
    finally:
        invoice.letterhead.file.close()

    letterhead_reader = PdfReader(io.BytesIO(letterhead_bytes))
    lh_page = letterhead_reader.pages[0]
    page_w = float(lh_page.mediabox.width)
    page_h = float(lh_page.mediabox.height)

    layout = _merge_layout(invoice.layout_json or {})
    items = list(invoice.items.all().order_by('position', 'id'))

    # ── Pass 1: build overlay pages, capturing how many pages we need ─────────
    # We render to a buffer per-page so we can count them before stamping totals/footer
    overlay_buf = io.BytesIO()
    c = canvas.Canvas(overlay_buf, pagesize=(page_w, page_h))

    page_num = 1
    # ── Page 1 content ────────────────────────────────────────────────────────
    _draw_title(c, invoice, layout, page_w, page_h)
    _draw_number_block(c, invoice, layout, page_w, page_h)

    # Bill To
    bill_to = invoice.client_name
    if invoice.client_address:
        bill_to = f'{invoice.client_name}\n{invoice.client_address}' if invoice.client_name else invoice.client_address
    if invoice.client_email:
        bill_to = (bill_to + f'\n{invoice.client_email}').strip('\n')
    _draw_address_block(c, layout['bill_to'], page_w, page_h, 'Bill To', bill_to or '—')

    # Ship To (optional)
    ship_to = invoice.ship_to_address or invoice.client_address
    if ship_to:
        _draw_address_block(c, layout['ship_to'], page_w, page_h, 'Ship To', ship_to)

    # Items (may overflow)
    last_y, overflow = _draw_items_table(c, invoice, layout, page_w, page_h, items)

    # If everything fits on page 1, draw totals/bank/notes now
    if not overflow:
        _draw_totals(c, invoice, layout, page_w, page_h)
        _draw_bank_details(c, invoice, layout, page_w, page_h)
        _draw_notes(c, invoice, layout, page_w, page_h)

    total_pages_estimate = 1 if not overflow else 2  # placeholder, updated after loop
    _draw_page_footer(c, invoice, page_num, total_pages_estimate, page_w)

    # ── Overflow pages ────────────────────────────────────────────────────────
    remaining = overflow
    overflow_pages_rendered = []
    while remaining:
        c.showPage()
        page_num += 1
        # continue items table from near top (below where letterhead header ends)
        top_y = page_h * 0.80  # 20% from top leaves room for letterhead header
        last_y, remaining = _draw_items_table(c, invoice, layout, page_w, page_h,
                                              remaining, start_y=top_y)
        if not remaining:
            # Final page — flow totals/bank/notes below where the items ended.
            # last_y is the y-coordinate (from bottom) of the last item row's bottom border.
            # Place totals immediately below with a comfortable gap.
            totals_y = max(last_y - 30, 180)  # keep clear of the footer band
            _draw_totals(c, invoice, layout, page_w, page_h, override_y=totals_y)
            # Bank details below totals on left side
            bank_y = max(totals_y - 90, 120)
            _draw_bank_details(c, invoice, layout, page_w, page_h, override_y=bank_y)
            # Notes at the very bottom
            notes_y = max(bank_y - 50, 80)
            _draw_notes(c, invoice, layout, page_w, page_h, override_y=notes_y)
        _draw_page_footer(c, invoice, page_num, page_num, page_w)  # will patch page counts below
        overflow_pages_rendered.append(page_num)

    c.save()
    overlay_buf.seek(0)

    # ── Pass 2: merge overlay onto repeated letterhead pages ──────────────────
    overlay_reader = PdfReader(overlay_buf)
    writer = PdfWriter()

    total_pages = len(overlay_reader.pages)
    for i in range(total_pages):
        # Fresh copy of letterhead page 1 for every output page
        base_reader = PdfReader(io.BytesIO(letterhead_bytes))
        base_page = base_reader.pages[0]
        overlay_page = overlay_reader.pages[i]
        base_page.merge_page(overlay_page)
        writer.add_page(base_page)

    out = io.BytesIO()
    writer.write(out)
    out.seek(0)
    return out.read()
