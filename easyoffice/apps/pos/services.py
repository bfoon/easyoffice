"""
apps/pos/services.py
────────────────────
Business logic: completing sales (stock + receipt number, atomically),
voiding (stock restore), matching walk-in details to existing contacts,
and the 80mm-style receipt PDF + email.
"""
from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation
from io import BytesIO

from django.conf import settings
from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from . import inventory
from .models import POSSale, POSSaleItem, POSDailyCounter

logger = logging.getLogger(__name__)


class POSError(Exception):
    """User-facing POS failure (insufficient stock, bad state, …)."""


def D(value, default='0.00') -> Decimal:
    try:
        return Decimal(str(value)).quantize(Decimal('0.01'))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(default)


# ─────────────────────────────────────────────────────────────────────────────
# Basket operations
# ─────────────────────────────────────────────────────────────────────────────

def get_or_create_open_basket(user) -> POSSale:
    """One open draft per cashier — reload-safe."""
    sale = (POSSale.objects
            .filter(cashier=user, status=POSSale.Status.DRAFT)
            .order_by('-created_at')
            .first())
    return sale or POSSale.objects.create(cashier=user)


def add_item(sale: POSSale, product: dict, qty: int = 1) -> POSSaleItem:
    """Add a product dict (from inventory adapter) — merges same-product lines."""
    if not sale.is_draft:
        raise POSError('This sale is already closed.')
    qty = max(1, int(qty))

    line = None
    if product.get('inventory_id'):
        line = sale.items.filter(inventory_id=product['inventory_id']).first()

    if line:
        line.quantity += qty
        line.save()
    else:
        line = POSSaleItem.objects.create(
            sale=sale,
            inventory_id=product.get('inventory_id', ''),
            sku=product.get('sku', ''),
            barcode=product.get('barcode', ''),
            name=product.get('name') or 'Item',
            unit_price=D(product.get('price')),
            quantity=qty,
        )
    sale.recalc_totals()
    return line


def set_quantity(sale: POSSale, item_id, qty: int) -> None:
    if not sale.is_draft:
        raise POSError('This sale is already closed.')
    line = sale.items.filter(pk=item_id).first()
    if not line:
        raise POSError('Line not found.')
    qty = int(qty)
    if qty <= 0:
        line.delete()
    else:
        line.quantity = qty
        line.save()
    sale.recalc_totals()


def remove_item(sale: POSSale, item_id) -> None:
    set_quantity(sale, item_id, 0)


def set_discount(sale: POSSale, amount) -> None:
    if not sale.is_draft:
        raise POSError('This sale is already closed.')
    amount = D(amount)
    if amount < 0:
        amount = Decimal('0.00')
    if amount > sale.subtotal:
        amount = sale.subtotal
    sale.discount = amount
    sale.save(update_fields=['discount', 'updated_at'])
    sale.recalc_totals()


# ─────────────────────────────────────────────────────────────────────────────
# Customer matching (optional capture)
# ─────────────────────────────────────────────────────────────────────────────

def match_existing_customer(phone: str = '', email: str = ''):
    """
    Try to link the walk-in to an existing customer_service.Customer by
    normalised phone or email. Lazy import; never raises.
    """
    try:
        from apps.customer_service.models import Customer, CustomerPhone, normalize_phone
    except Exception:
        return None

    phone = (phone or '').strip()
    email = (email or '').strip().lower()

    if phone:
        try:
            row = (CustomerPhone.objects
                   .select_related('customer')
                   .filter(normalized_number=normalize_phone(phone), is_active=True)
                   .first())
            if row:
                return row.customer
        except Exception:
            pass
    if email:
        try:
            hit = Customer.objects.filter(email__iexact=email).first()
            if hit:
                return hit
        except Exception:
            pass
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Completion & void
# ─────────────────────────────────────────────────────────────────────────────

@transaction.atomic
def complete_sale(sale: POSSale, *, payment_method: str, amount_tendered=None,
                  payment_ref: str = '', customer_name: str = '',
                  customer_phone: str = '', customer_email: str = '') -> POSSale:
    """
    Finalise the basket atomically:
      1. lock the sale row (double-click / two-tab protection)
      2. deduct stock per line — insufficient stock rolls everything back
      3. assign the receipt number
      4. store payment + optional customer details
    """
    sale = POSSale.objects.select_for_update().get(pk=sale.pk)
    if sale.status != POSSale.Status.DRAFT:
        raise POSError('This sale was already completed or voided.')

    lines = list(sale.items.all())
    if not lines:
        raise POSError('The basket is empty — scan or add at least one item.')

    sale.recalc_totals(save=False)

    if payment_method not in POSSale.Payment.values:
        payment_method = POSSale.Payment.CASH

    tendered = D(amount_tendered) if amount_tendered not in (None, '') else None
    change = None
    if payment_method == POSSale.Payment.CASH:
        if tendered is None:
            tendered = sale.total
        if tendered < sale.total:
            raise POSError(
                f'Cash received ({tendered}) is less than the total ({sale.total}).'
            )
        change = (tendered - sale.total).quantize(Decimal('0.01'))

    # Receipt number FIRST, so every stock movement carries it as source_ref.
    sale.sale_no = POSDailyCounter.next_sale_no()

    # ── Stock deduction — each line becomes a SALE movement in the
    #    inventory ledger (source_kind='pos_sale', source_ref=receipt no).
    #    The availability guard runs under lock, so two tills can't both
    #    sell the last unit. Any failure rolls back the whole sale,
    #    movements and receipt number included.
    for line in lines:
        if not line.inventory_id:
            continue  # ad-hoc/manual line — no stock behind it
        if not inventory.deduct_stock(line.inventory_id, line.quantity,
                                      actor=sale.cashier,
                                      source_ref=sale.sale_no):
            raise POSError(
                f'Not enough stock for "{line.name}" '
                f'(requested {line.quantity}, available '
                f'{inventory.current_stock(line.inventory_id) or 0}).'
            )
        line.stock_deducted = True
        line.save(update_fields=['stock_deducted', 'updated_at'])

    sale.status = POSSale.Status.COMPLETED
    sale.completed_at = timezone.now()
    sale.payment_method = payment_method
    sale.payment_ref = (payment_ref or '')[:80]
    sale.amount_tendered = tendered
    sale.change_due = change

    sale.customer_name  = (customer_name or '')[:180]
    sale.customer_phone = (customer_phone or '')[:30]
    sale.customer_email = (customer_email or '').strip()[:254]
    sale.customer = match_existing_customer(sale.customer_phone, sale.customer_email)

    # Link to the cashier's open cash session (if any) so the drawer
    # reconciliation tallies this sale. Lazy import avoids a cycle.
    try:
        from .services_cash import active_session
        session = active_session(sale.cashier)
        if session is not None:
            sale.session = session
    except Exception:
        logger.exception('POS: could not link sale to cash session')

    sale.save()

    if sale.customer_email:
        transaction.on_commit(lambda: email_receipt(sale))

    return sale


@transaction.atomic
def void_sale(sale: POSSale, *, actor, reason: str = '') -> POSSale:
    """Void a completed sale and put the stock back."""
    sale = POSSale.objects.select_for_update().get(pk=sale.pk)
    if sale.status != POSSale.Status.COMPLETED:
        raise POSError('Only completed sales can be voided.')

    for line in sale.items.filter(stock_deducted=True):
        inventory.restore_stock(line.inventory_id, line.quantity,
                                actor=actor, source_ref=sale.sale_no or '')
        line.stock_deducted = False
        line.save(update_fields=['stock_deducted', 'updated_at'])

    sale.status = POSSale.Status.VOIDED
    sale.voided_at = timezone.now()
    sale.voided_by = actor
    sale.void_reason = (reason or '')[:300]
    sale.save(update_fields=['status', 'voided_at', 'voided_by',
                             'void_reason', 'updated_at'])
    return sale


# ─────────────────────────────────────────────────────────────────────────────
# Receipt (PDF + email)
# ─────────────────────────────────────────────────────────────────────────────

def _org():
    return {
        'name':    getattr(settings, 'ORGANISATION_NAME', 'Easy Solutions'),
        'address': getattr(settings, 'ORGANISATION_ADDRESS', 'Banjul, The Gambia'),
        'phone':   getattr(settings, 'ORGANISATION_PHONE', ''),
        'email':   getattr(settings, 'ORGANISATION_EMAIL', ''),
    }


def public_receipt_url(sale: POSSale, request=None) -> str:
    path = reverse('pos_receipt_public', kwargs={'token': sale.receipt_token})
    if request is not None:
        return request.build_absolute_uri(path)
    base = (getattr(settings, 'SITE_URL', '') or '').rstrip('/')
    return f'{base}{path}' if base else path


def build_receipt_pdf(sale: POSSale) -> bytes:
    """
    Slim thermal-style receipt (80 mm wide) as a PDF.
    Includes a QR code pointing at the public digital receipt.
    """
    from reportlab.lib.units import mm
    from reportlab.pdfgen import canvas
    from reportlab.graphics.barcode import qr
    from reportlab.graphics.shapes import Drawing
    from reportlab.graphics import renderPDF

    org = _org()
    lines = list(sale.items.all())

    width = 80 * mm
    # Height grows with the number of lines: ~95mm of header/totals/footer,
    # ~8mm per product line, plus ~60mm for the QR block.
    height = (95 + len(lines) * 8 + 60) * mm

    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=(width, height))
    y = height - 10 * mm
    cx = width / 2

    def center(text, font='Helvetica', size=8, dy=4 * mm):
        nonlocal y
        c.setFont(font, size)
        c.drawCentredString(cx, y, text)
        y -= dy

    def row(left, right, font='Helvetica', size=8, dy=4 * mm):
        nonlocal y
        c.setFont(font, size)
        c.drawString(4 * mm, y, left)
        c.drawRightString(width - 4 * mm, y, right)
        y -= dy

    def rule(dy=3 * mm):
        nonlocal y
        c.setDash(1, 2)
        c.line(4 * mm, y, width - 4 * mm, y)
        c.setDash()
        y -= dy

    center(org['name'], 'Helvetica-Bold', 11, 5 * mm)
    if org['address']:
        center(org['address'], size=7)
    if org['phone']:
        center(f"Tel: {org['phone']}", size=7)
    rule()

    center('SALES RECEIPT', 'Helvetica-Bold', 9, 5 * mm)
    row('Receipt No:', sale.sale_no or '—', 'Helvetica-Bold')
    row('Date:', timezone.localtime(sale.completed_at or sale.created_at)
        .strftime('%d %b %Y  %H:%M'))
    row('Cashier:', getattr(sale.cashier, 'get_full_name', lambda: '')() or
        getattr(sale.cashier, 'username', '—'))
    if sale.customer_name:
        row('Customer:', sale.customer_name)
    if sale.customer_phone:
        row('Phone:', sale.customer_phone)
    rule()

    for line in lines:
        c.setFont('Helvetica-Bold', 8)
        c.drawString(4 * mm, y, line.name[:38])
        y -= 3.6 * mm
        row(f'  {line.quantity} × {line.unit_price}',
            f'{sale.currency} {line.line_total}', size=8)
    rule()

    row('Subtotal:', f'{sale.currency} {sale.subtotal}')
    if sale.discount:
        row('Discount:', f'-{sale.currency} {sale.discount}')
    if sale.tax:
        row('Tax:', f'{sale.currency} {sale.tax}')
    row('TOTAL:', f'{sale.currency} {sale.total}', 'Helvetica-Bold', 10, 5 * mm)
    row('Paid by:', sale.get_payment_method_display())
    if sale.payment_ref:
        row('Ref:', sale.payment_ref)
    if sale.amount_tendered is not None:
        row('Tendered:', f'{sale.currency} {sale.amount_tendered}')
    if sale.change_due is not None:
        row('Change:', f'{sale.currency} {sale.change_due}')
    rule()

    # QR → digital receipt
    try:
        url = public_receipt_url(sale)
        code = qr.QrCodeWidget(url)
        b = code.getBounds()
        size = 26 * mm
        d = Drawing(size, size,
                    transform=[size / (b[2] - b[0]), 0, 0,
                               size / (b[3] - b[1]), 0, 0])
        d.add(code)
        renderPDF.draw(d, c, cx - size / 2, y - size)
        y -= size + 4 * mm
        center('Scan for your digital receipt', size=6.5)
    except Exception:
        logger.warning('POS: QR render failed for %s', sale.pk, exc_info=True)

    center('Thank you for your business!', 'Helvetica-Oblique', 8, 5 * mm)
    c.showPage()
    c.save()
    return buf.getvalue()


def email_receipt(sale: POSSale) -> bool:
    """PDF receipt to the customer's email. Best-effort."""
    if not sale.customer_email:
        return False
    try:
        from django.core.mail import EmailMessage
        org = _org()
        pdf = build_receipt_pdf(sale)
        msg = EmailMessage(
            subject=f'Your receipt {sale.sale_no} — {org["name"]}',
            body=(
                f'Hello{(" " + sale.customer_name) if sale.customer_name else ""},\n\n'
                f'Thank you for your purchase. Your receipt {sale.sale_no} is attached.\n'
                f'You can also view it online:\n\n  {public_receipt_url(sale)}\n\n'
                f'— {org["name"]}'
            ),
            from_email=getattr(settings, 'DEFAULT_FROM_EMAIL', None) or 'no-reply@localhost',
            to=[sale.customer_email],
        )
        msg.attach(f'{sale.sale_no}.pdf', pdf, 'application/pdf')
        msg.send(fail_silently=False)
        sale.receipt_emailed_at = timezone.now()
        sale.save(update_fields=['receipt_emailed_at', 'updated_at'])
        return True
    except Exception:
        logger.exception('POS: receipt email failed for sale %s', sale.pk)
        return False
