"""
apps/pos/views.py
─────────────────
Views for the walk-in Point of Sale.

House style: class-based views with manual POST handling (no Django
forms), JSON endpoints for the terminal, lazy imports, 404-over-403 on
public token views.

Endpoints
─────────
  GET  /pos/                      terminal page (open basket auto-created)
  GET  /pos/api/lookup/?code=…    exact barcode/SKU hit (scanner path)
  GET  /pos/api/search/?q=…       type-ahead product search
  POST /pos/api/basket/add/       {code|inventory_id|manual…, qty}
  POST /pos/api/basket/qty/       {item_id, qty}        (0 removes)
  POST /pos/api/basket/discount/  {amount}
  POST /pos/api/basket/clear/
  POST /pos/api/complete/         payment + optional customer details
  GET  /pos/receipt/<pk>/         printable receipt (staff)
  GET  /pos/receipt/<pk>/pdf/     receipt PDF download (staff)
  POST /pos/receipt/<pk>/email/   send/re-send digital receipt (staff)
  GET  /pos/r/<token>/            PUBLIC digital receipt (QR target)
  GET  /pos/sales/                today's sales + Z-summary
  POST /pos/sales/<pk>/void/      void + restore stock (supervisors)
"""
from __future__ import annotations

import json
import logging

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.db.models import Count, Sum
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.generic import View, TemplateView

from . import inventory, services
from .models import POSSale
from .services import POSError

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Access control
# ─────────────────────────────────────────────────────────────────────────────

POS_GROUPS = {'pos', 'cashier', 'point of sale', 'sales'}


def can_use_pos(user) -> bool:
    """Cashiers, the CS team, and admins. Mirrors the CS predicate style."""
    if not user or not getattr(user, 'is_authenticated', False):
        return False
    if user.is_superuser or user.is_staff:
        return True
    groups = {g.lower() for g in user.groups.values_list('name', flat=True)}
    if groups & POS_GROUPS:
        return True
    try:
        from apps.customer_service.permissions import can_use_customer_service
        return can_use_customer_service(user)
    except Exception:
        return False


def can_void_sale(user) -> bool:
    """Voids are supervisor-level: heads/managers/superusers."""
    if not user or not getattr(user, 'is_authenticated', False):
        return False
    if user.is_superuser:
        return True
    groups = {g.lower() for g in user.groups.values_list('name', flat=True)}
    if groups & {'manager', 'supervisor', 'admin', 'administrator', 'ceo',
                 'head of customer service', 'head of sales'}:
        return True
    try:
        from apps.customer_service.permissions import is_head_of_customer_service, is_head_of_sales
        return is_head_of_customer_service(user) or is_head_of_sales(user)
    except Exception:
        return False


class POSAccessMixin(LoginRequiredMixin):
    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated and not can_use_pos(request.user):
            raise PermissionDenied('You do not have access to the Point of Sale.')
        return super().dispatch(request, *args, **kwargs)


def _json_body(request) -> dict:
    try:
        data = json.loads((request.body or b'{}').decode('utf-8'))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _basket_payload(sale: POSSale) -> dict:
    return {
        'sale_id': str(sale.pk),
        'status': sale.status,
        'currency': sale.currency,
        'items': [
            {
                'item_id': str(i.pk),
                'inventory_id': i.inventory_id,
                'name': i.name,
                'sku': i.sku,
                'barcode': i.barcode,
                'unit_price': str(i.unit_price),
                'quantity': i.quantity,
                'line_total': str(i.line_total),
                'stock': inventory.current_stock(i.inventory_id)
                         if i.inventory_id else None,
            }
            for i in sale.items.all()
        ],
        'subtotal': str(sale.subtotal),
        'discount': str(sale.discount),
        'total': str(sale.total),
        'item_count': sale.item_count,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Terminal page
# ─────────────────────────────────────────────────────────────────────────────

class POSTerminalView(POSAccessMixin, TemplateView):
    template_name = 'pos/pos_terminal.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        sale = services.get_or_create_open_basket(self.request.user)
        ctx['sale'] = sale
        ctx['basket_json'] = json.dumps(_basket_payload(sale))
        ctx['payment_methods'] = POSSale.Payment.choices
        ctx['currency'] = sale.currency
        return ctx


# ─────────────────────────────────────────────────────────────────────────────
# Product lookup (scan) and search
# ─────────────────────────────────────────────────────────────────────────────

class ProductLookupView(POSAccessMixin, View):
    """Exact barcode/SKU lookup — what the scanner input calls on Enter."""

    def get(self, request):
        code = (request.GET.get('code') or '').strip()
        if not code:
            return JsonResponse({'ok': False, 'error': 'empty_code'}, status=400)
        product = inventory.lookup_by_code(code)
        if not product:
            return JsonResponse({'ok': False, 'error': 'not_found', 'code': code},
                                status=404)
        return JsonResponse({'ok': True, 'product': product})


class ProductSearchView(POSAccessMixin, View):
    """Type-ahead by name/SKU/barcode for when there's no scannable code."""

    def get(self, request):
        q = (request.GET.get('q') or '').strip()
        return JsonResponse({'ok': True, 'results': inventory.search(q)})


# ─────────────────────────────────────────────────────────────────────────────
# Basket API
# ─────────────────────────────────────────────────────────────────────────────

class BasketAddView(POSAccessMixin, View):
    """
    Add a line. Accepts any of:
      {code}                        → scan path (barcode/SKU)
      {inventory_id}                → picked from search results
      {manual_name, manual_price}   → ad-hoc line with no stock behind it
    plus optional {qty} (default 1).
    """

    def post(self, request):
        data = _json_body(request)
        sale = services.get_or_create_open_basket(request.user)
        qty = data.get('qty') or 1

        product = None
        if data.get('code'):
            product = inventory.lookup_by_code(str(data['code']))
            if not product:
                return JsonResponse({'ok': False, 'error': 'not_found',
                                     'code': data['code']}, status=404)
        elif data.get('inventory_id'):
            item = inventory.get_item(str(data['inventory_id']))
            if not item:
                return JsonResponse({'ok': False, 'error': 'not_found'}, status=404)
            product = inventory.serialize(item)
        elif data.get('manual_name'):
            product = {
                'inventory_id': '',
                'name': str(data['manual_name'])[:255],
                'sku': '', 'barcode': '',
                'price': str(services.D(data.get('manual_price'))),
                'stock': None,
            }
        else:
            return JsonResponse({'ok': False, 'error': 'nothing_to_add'}, status=400)

        # Soft stock warning at scan time (hard guard happens at completion)
        warning = ''
        if product.get('inventory_id') and product.get('stock') is not None:
            in_basket = 0
            existing = sale.items.filter(inventory_id=product['inventory_id']).first()
            if existing:
                in_basket = existing.quantity
            if product['stock'] < in_basket + int(qty):
                warning = (f'Low stock: only {product["stock"]} of '
                           f'"{product["name"]}" on hand.')

        try:
            services.add_item(sale, product, qty=int(qty))
        except POSError as e:
            return JsonResponse({'ok': False, 'error': str(e)}, status=400)

        return JsonResponse({'ok': True, 'warning': warning,
                             'basket': _basket_payload(sale)})


class BasketQtyView(POSAccessMixin, View):
    def post(self, request):
        data = _json_body(request)
        sale = services.get_or_create_open_basket(request.user)
        try:
            services.set_quantity(sale, data.get('item_id'), int(data.get('qty', 1)))
        except (POSError, ValueError, TypeError) as e:
            return JsonResponse({'ok': False, 'error': str(e)}, status=400)
        return JsonResponse({'ok': True, 'basket': _basket_payload(sale)})


class BasketDiscountView(POSAccessMixin, View):
    def post(self, request):
        data = _json_body(request)
        sale = services.get_or_create_open_basket(request.user)
        try:
            services.set_discount(sale, data.get('amount'))
        except POSError as e:
            return JsonResponse({'ok': False, 'error': str(e)}, status=400)
        return JsonResponse({'ok': True, 'basket': _basket_payload(sale)})


class BasketClearView(POSAccessMixin, View):
    def post(self, request):
        sale = services.get_or_create_open_basket(request.user)
        if sale.is_draft:
            sale.items.all().delete()
            sale.discount = 0
            sale.save(update_fields=['discount', 'updated_at'])
            sale.recalc_totals()
        return JsonResponse({'ok': True, 'basket': _basket_payload(sale)})


# ─────────────────────────────────────────────────────────────────────────────
# Completion
# ─────────────────────────────────────────────────────────────────────────────

class CompleteSaleView(POSAccessMixin, View):
    """
    Body: {payment_method, amount_tendered?, payment_ref?,
           customer_name?, customer_phone?, customer_email?}

    Customer fields are all optional — name+phone personalise the printed
    receipt, an email address triggers the digital PDF receipt.
    """

    def post(self, request):
        data = _json_body(request)
        sale = services.get_or_create_open_basket(request.user)

        # Optional: require an open cash session before a CASH sale, so the
        # drawer balance always tallies. Enable with POS_REQUIRE_CASH_SESSION.
        from django.conf import settings
        method = (data.get('payment_method') or 'cash').lower()
        if getattr(settings, 'POS_REQUIRE_CASH_SESSION', False) and method == 'cash':
            from . import services_cash
            if services_cash.active_session(request.user) is None:
                return JsonResponse(
                    {'ok': False, 'error': 'no_session',
                     'message': 'Open the cash drawer before taking cash payments.'},
                    status=409)

        try:
            sale = services.complete_sale(
                sale,
                payment_method=(data.get('payment_method') or 'cash').lower(),
                amount_tendered=data.get('amount_tendered'),
                payment_ref=data.get('payment_ref', ''),
                customer_name=data.get('customer_name', ''),
                customer_phone=data.get('customer_phone', ''),
                customer_email=data.get('customer_email', ''),
            )
        except POSError as e:
            return JsonResponse({'ok': False, 'error': str(e)}, status=400)
        except Exception:
            logger.exception('POS: complete_sale crashed')
            return JsonResponse({'ok': False,
                                 'error': 'Something went wrong completing the sale.'},
                                status=500)

        from django.urls import reverse
        return JsonResponse({
            'ok': True,
            'sale_no': sale.sale_no,
            'total': str(sale.total),
            'change_due': str(sale.change_due) if sale.change_due is not None else None,
            'receipt_url': reverse('pos_receipt', kwargs={'pk': sale.pk}),
            'receipt_pdf_url': reverse('pos_receipt_pdf', kwargs={'pk': sale.pk}),
            'emailed': bool(sale.customer_email),
        })


# ─────────────────────────────────────────────────────────────────────────────
# Receipts
# ─────────────────────────────────────────────────────────────────────────────

class ReceiptView(POSAccessMixin, TemplateView):
    """Printable receipt page (opens print dialog automatically)."""
    template_name = 'pos/receipt.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        sale = get_object_or_404(POSSale, pk=kwargs['pk'])
        ctx.update(self._receipt_ctx(sale, self.request))
        ctx['auto_print'] = True
        return ctx

    @staticmethod
    def _receipt_ctx(sale, request=None):
        return {
            'sale': sale,
            'org': services._org(),
            'public_url': services.public_receipt_url(sale, request=request),
        }


class ReceiptPDFView(POSAccessMixin, View):
    def get(self, request, pk):
        sale = get_object_or_404(POSSale, pk=pk)
        pdf = services.build_receipt_pdf(sale)
        response = HttpResponse(pdf, content_type='application/pdf')
        try:
            from apps.files.security_utils import set_content_disposition
            set_content_disposition(response, f'{sale.sale_no or "receipt"}.pdf',
                                    inline=True)
        except Exception:
            response['Content-Disposition'] = (
                f'inline; filename="{(sale.sale_no or "receipt")}.pdf"')
        response['X-Content-Type-Options'] = 'nosniff'
        return response


class ReceiptEmailView(POSAccessMixin, View):
    """Send / re-send the digital receipt. Body: {email?} to override."""

    def post(self, request, pk):
        sale = get_object_or_404(POSSale, pk=pk)
        data = _json_body(request)
        new_email = (data.get('email') or '').strip()
        if new_email:
            sale.customer_email = new_email[:254]
            sale.customer = services.match_existing_customer(
                sale.customer_phone, sale.customer_email) or sale.customer
            sale.save(update_fields=['customer_email', 'customer', 'updated_at'])
        if not sale.customer_email:
            return JsonResponse({'ok': False,
                                 'error': 'No email address on this sale.'}, status=400)
        sent = services.email_receipt(sale)
        return JsonResponse({'ok': sent,
                             'error': '' if sent else 'Email failed — check logs.'})


class ReceiptPublicView(TemplateView):
    """
    PUBLIC digital receipt — the QR code on the paper receipt points here.
    No login; the unguessable token is the authorisation. 404 on anything
    that isn't a completed sale (no existence leak).
    """
    template_name = 'pos/receipt.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        sale = (POSSale.objects
                .filter(receipt_token=kwargs['token'],
                        status=POSSale.Status.COMPLETED)
                .first())
        if not sale:
            raise Http404
        ctx.update(ReceiptView._receipt_ctx(sale, self.request))
        ctx['auto_print'] = False
        ctx['is_public'] = True
        return ctx


# ─────────────────────────────────────────────────────────────────────────────
# Sales list / day summary / void
# ─────────────────────────────────────────────────────────────────────────────

class SalesTodayView(POSAccessMixin, TemplateView):
    """Today's sales with a running Z-summary (count, gross, by method)."""
    template_name = 'pos/sales_today.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        today = timezone.localdate()
        qs = (POSSale.objects
              .filter(completed_at__date=today)
              .select_related('cashier')
              .prefetch_related('items'))
        completed = qs.filter(status=POSSale.Status.COMPLETED)
        ctx['sales'] = qs.order_by('-completed_at')
        ctx['summary'] = completed.aggregate(
            count=Count('id'), gross=Sum('total'))
        ctx['by_method'] = (completed.values('payment_method')
                            .annotate(count=Count('id'), total=Sum('total'))
                            .order_by('-total'))
        ctx['can_void'] = can_void_sale(self.request.user)
        ctx['today'] = today
        return ctx


class VoidSaleView(POSAccessMixin, View):
    def post(self, request, pk):
        if not can_void_sale(request.user):
            raise PermissionDenied('Voiding a sale requires a supervisor.')
        sale = get_object_or_404(POSSale, pk=pk)
        reason = (request.POST.get('reason') or
                  _json_body(request).get('reason') or '').strip()
        try:
            services.void_sale(sale, actor=request.user, reason=reason)
        except POSError as e:
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return JsonResponse({'ok': False, 'error': str(e)}, status=400)
            messages.error(request, str(e))
            return redirect('pos_sales_today')

        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return JsonResponse({'ok': True})
        messages.success(request,
                         f'Sale {sale.sale_no} voided — stock restored.')
        return redirect('pos_sales_today')
