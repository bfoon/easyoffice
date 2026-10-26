"""
apps/investments/services/profit.py
───────────────────────────────────
Works out the profit made from selling an opportunity's items in a window.

Sources (each optional per opportunity):
  • POS       — completed POSSale lines whose inventory_id is a linked
                product. Revenue is the line total less its share of the
                basket discount; tax is excluded.
  • Invoices  — doc_type=invoice, status=finalized, not voided, dated by
                paid_at (cash basis, default) or finalized_at. Lines are
                matched to products by exact name or SKU, exactly like
                apps/invoices/stock.py. Revenue is net of the invoice
                discount (pro-rata); tax excluded.
  • Manual    — ProfitAdjustment rows (extra revenue / expenses).

Cost of goods = quantity × Product.cost_price at calculation time.

Each source is imported lazily and fails soft: if POS or invoices are not
installed, that source contributes nothing and the breakdown says so.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from decimal import Decimal

from django.utils import timezone

logger = logging.getLogger(__name__)

ZERO = Decimal('0.00')
CENT = Decimal('0.01')


@dataclass
class SaleLine:
    product_id: str
    qty: Decimal
    revenue: Decimal
    cost: Decimal
    source: str
    ref: str
    at: datetime


def _products(opportunity):
    return {str(p.pk): p for p in opportunity.products.all()}


def _q(v):
    return Decimal(v).quantize(CENT)


def pos_lines(products: dict, start, end):
    try:
        from apps.pos.models import POSSale, POSSaleItem
    except Exception:
        return None
    if not products:
        return []
    out = []
    items = (POSSaleItem.objects
             .filter(sale__status=POSSale.Status.COMPLETED,
                     sale__completed_at__gte=start, sale__completed_at__lt=end,
                     inventory_id__in=list(products.keys()))
             .select_related('sale'))
    for it in items:
        sale = it.sale
        factor = Decimal('1')
        if sale.subtotal and sale.discount:
            factor = max(Decimal('0'), (sale.subtotal - sale.discount) / sale.subtotal)
        product = products[it.inventory_id]
        qty = Decimal(it.quantity)
        out.append(SaleLine(
            product_id=it.inventory_id, qty=qty,
            revenue=_q(Decimal(it.line_total) * factor),
            cost=_q(qty * (product.cost_price or ZERO)),
            source='pos', ref=sale.sale_no or str(sale.pk)[:8], at=sale.completed_at,
        ))
    return out


def invoice_lines(products: dict, start, end, basis='paid'):
    try:
        from apps.invoices.models import DocType, InvoiceDocument
    except Exception:
        return None
    if not products:
        return []
    by_name = {}
    for pid, p in products.items():
        by_name[(p.name or '').strip().lower()] = pid
        by_name[(p.sku or '').strip().lower()] = pid
    by_name.pop('', None)

    date_field = 'paid_at' if basis == 'paid' else 'finalized_at'
    docs = (InvoiceDocument.objects
            .filter(doc_type=DocType.INVOICE, status=InvoiceDocument.Status.FINALIZED,
                    voided_at__isnull=True,
                    **{f'{date_field}__gte': start, f'{date_field}__lt': end})
            .prefetch_related('items'))
    out = []
    for doc in docs:
        factor = Decimal('1')
        if doc.subtotal and doc.discount_amount:
            factor = max(Decimal('0'), (doc.subtotal - doc.discount_amount) / doc.subtotal)
        for line in doc.items.all():
            pid = by_name.get((line.description or '').strip().lower())
            if not pid:
                continue
            product = products[pid]
            qty = Decimal(line.quantity)
            out.append(SaleLine(
                product_id=pid, qty=qty,
                revenue=_q(Decimal(line.line_total) * factor),
                cost=_q(qty * (product.cost_price or ZERO)),
                source='invoice', ref=doc.number or str(doc.pk)[:8],
                at=getattr(doc, date_field),
            ))
    return out


def adjustments_net(opportunity, start, end):
    """Manual adjustments dated inside [start, end) (dates compared locally)."""
    s, e = timezone.localtime(start).date(), timezone.localtime(end).date()
    total, rows = ZERO, []
    for adj in opportunity.adjustments.filter(date__gte=s, date__lt=e):
        total += adj.signed_amount
        rows.append({'date': adj.date.isoformat(), 'kind': adj.kind,
                     'amount': str(adj.amount), 'description': adj.description})
    return total, rows


def compute_profit(opportunity, start, end, *, include_adjustments=True,
                   ignore_sales_start=False) -> dict:
    """Profit summary for sales of the opportunity's items in [start, end)."""
    if not ignore_sales_start and opportunity.sales_start:
        floor = timezone.make_aware(datetime.combine(opportunity.sales_start, time.min))
        if end <= floor:
            start = end
        elif start < floor:
            start = floor

    products = _products(opportunity)
    lines, notes = [], []
    if opportunity.include_pos:
        got = pos_lines(products, start, end)
        if got is None:
            notes.append('POS app not installed — POS sales not counted.')
        else:
            lines += got
    if opportunity.include_invoices:
        got = invoice_lines(products, start, end, opportunity.invoice_basis)
        if got is None:
            notes.append('Invoices app not installed — invoice sales not counted.')
        else:
            lines += got

    per_product = defaultdict(lambda: {'qty': ZERO, 'revenue': ZERO, 'cost': ZERO})
    per_source = defaultdict(lambda: {'count': 0, 'revenue': ZERO})
    revenue = cost = units = ZERO
    for ln in lines:
        revenue += ln.revenue
        cost += ln.cost
        units += ln.qty
        pp = per_product[ln.product_id]
        pp['qty'] += ln.qty
        pp['revenue'] += ln.revenue
        pp['cost'] += ln.cost
        per_source[ln.source]['count'] += 1
        per_source[ln.source]['revenue'] += ln.revenue

    adj_total, adj_rows = (ZERO, [])
    if include_adjustments:
        adj_total, adj_rows = adjustments_net(opportunity, start, end)

    net = (revenue - cost + adj_total).quantize(CENT)
    return {
        'revenue': revenue.quantize(CENT),
        'cost': cost.quantize(CENT),
        'adjustments_net': adj_total.quantize(CENT),
        'net_profit': net,
        'units_sold': units,
        'breakdown': {
            'products': [
                {'id': pid, 'name': products[pid].name, 'sku': products[pid].sku,
                 'qty': str(v['qty']), 'revenue': str(v['revenue'].quantize(CENT)),
                 'cost': str(v['cost'].quantize(CENT)),
                 'profit': str((v['revenue'] - v['cost']).quantize(CENT))}
                for pid, v in sorted(per_product.items(), key=lambda kv: -kv[1]['revenue'])
            ],
            'sources': {k: {'count': v['count'], 'revenue': str(v['revenue'].quantize(CENT))}
                        for k, v in per_source.items()},
            'adjustments': adj_rows,
            'notes': notes,
        },
    }


def trailing_profit(opportunity, days=90):
    """Historic profit of the items over the last `days`, ignoring sales_start.
    Used to estimate returns for a listing that has no payouts yet."""
    end = timezone.now()
    start = end - timedelta(days=days)
    return compute_profit(opportunity, start, end, include_adjustments=False,
                          ignore_sales_start=True)
