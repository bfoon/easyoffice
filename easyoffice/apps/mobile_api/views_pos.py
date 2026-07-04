"""
apps/mobile_api/views_pos.py
────────────────────────────
JWT-authenticated POS endpoints for the mobile app — the phone becomes a
handheld till. These are thin wrappers around apps/pos/services.py and
apps/pos/inventory.py, so web terminal and phone share ONE code path:
the same draft basket per cashier (start a sale on the phone, finish it
on the desktop till — it's the same basket), the same ledger movements,
the same receipt numbers.

Routes (added to apps/mobile_api/urls.py):

    GET  pos/lookup/?code=…        exact barcode / SKU / QR-token hit
    GET  pos/search/?q=…           type-ahead by name / SKU / barcode
    GET  pos/basket/               current open basket
    POST pos/basket/add/           {code | inventory_id | manual_name+manual_price, qty}
    POST pos/basket/qty/           {item_id, qty}   (0 removes the line)
    POST pos/basket/discount/      {amount}
    POST pos/basket/clear/
    POST pos/complete/             payment + optional customer details
    POST pos/receipt/<pk>/email/   send / re-send digital receipt
    GET  pos/sales/today/          the rep's day summary
"""

import logging

from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.utils import timezone

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Shared bits
# ─────────────────────────────────────────────────────────────────────────────

def _pos():
    """Lazy import of the POS app internals (house style)."""
    from apps.pos import inventory, services
    from apps.pos.views import can_use_pos, _basket_payload
    return inventory, services, can_use_pos, _basket_payload


class POSAPIView(APIView):
    """Base: JWT auth + the same can_use_pos gate the web terminal uses."""
    permission_classes = [IsAuthenticated]

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        _, _, can_use_pos, _ = _pos()
        if not can_use_pos(request.user):
            from rest_framework.exceptions import PermissionDenied
            raise PermissionDenied('You do not have access to the Point of Sale.')


def _abs(request, path):
    try:
        return request.build_absolute_uri(path)
    except Exception:
        return path


# ─────────────────────────────────────────────────────────────────────────────
# Product lookup / search
# ─────────────────────────────────────────────────────────────────────────────

class MobilePOSLookupView(POSAPIView):
    """
    GET /api/mobile/v1/pos/lookup/?code=<scanned>

    The scan path. The phone camera can hand over anything printed in the
    shop: an EAN/UPC barcode, a typed SKU, a product-label QR token, a
    shelf/bin-label QR token — or the full label URL those QRs encode.
    """

    def get(self, request):
        inventory, *_ = _pos()
        code = (request.query_params.get('code') or '').strip()
        if not code:
            return Response({'ok': False, 'error': 'empty_code'}, status=400)
        product = inventory.lookup_by_code(code)
        if not product:
            return Response({'ok': False, 'error': 'not_found', 'code': code},
                            status=404)
        return Response({'ok': True, 'product': product})


class MobilePOSSearchView(POSAPIView):
    """GET /api/mobile/v1/pos/search/?q=<text> — name / SKU / barcode."""

    def get(self, request):
        inventory, *_ = _pos()
        q = (request.query_params.get('q') or '').strip()
        return Response({'ok': True, 'results': inventory.search(q)})


# ─────────────────────────────────────────────────────────────────────────────
# Basket
# ─────────────────────────────────────────────────────────────────────────────

class MobilePOSBasketView(POSAPIView):
    """GET /api/mobile/v1/pos/basket/ — the cashier's current open basket."""

    def get(self, request):
        _, services, _, _basket_payload = _pos()
        sale = services.get_or_create_open_basket(request.user)
        return Response({'ok': True, 'basket': _basket_payload(sale)})


class MobilePOSBasketAddView(POSAPIView):
    """
    POST /api/mobile/v1/pos/basket/add/
    Body: {code} | {inventory_id} | {manual_name, manual_price}, plus {qty}.
    """

    def post(self, request):
        inventory, services, _, _basket_payload = _pos()
        from apps.pos.services import POSError

        data = request.data or {}
        sale = services.get_or_create_open_basket(request.user)
        try:
            qty = max(1, int(data.get('qty') or 1))
        except (TypeError, ValueError):
            qty = 1

        product = None
        if data.get('code'):
            product = inventory.lookup_by_code(str(data['code']))
            if not product:
                return Response({'ok': False, 'error': 'not_found',
                                 'code': data['code']}, status=404)
        elif data.get('inventory_id'):
            item = inventory.get_item(str(data['inventory_id']))
            if not item:
                return Response({'ok': False, 'error': 'not_found'}, status=404)
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
            return Response({'ok': False, 'error': 'nothing_to_add'}, status=400)

        # Soft low-stock warning at scan time (hard guard at completion).
        warning = ''
        if product.get('inventory_id') and product.get('stock') is not None:
            existing = sale.items.filter(
                inventory_id=product['inventory_id']).first()
            in_basket = existing.quantity if existing else 0
            if product['stock'] < in_basket + qty:
                warning = (f'Low stock: only {product["stock"]} of '
                           f'"{product["name"]}" available here.')

        try:
            services.add_item(sale, product, qty=qty)
        except POSError as e:
            return Response({'ok': False, 'error': str(e)}, status=400)

        return Response({'ok': True, 'warning': warning,
                         'basket': _basket_payload(sale)})


class MobilePOSBasketQtyView(POSAPIView):
    """POST /api/mobile/v1/pos/basket/qty/  Body: {item_id, qty} (0 removes)."""

    def post(self, request):
        _, services, _, _basket_payload = _pos()
        from apps.pos.services import POSError

        sale = services.get_or_create_open_basket(request.user)
        try:
            services.set_quantity(sale, request.data.get('item_id'),
                                  int(request.data.get('qty', 1)))
        except (POSError, ValueError, TypeError) as e:
            return Response({'ok': False, 'error': str(e)}, status=400)
        return Response({'ok': True, 'basket': _basket_payload(sale)})


class MobilePOSBasketDiscountView(POSAPIView):
    """POST /api/mobile/v1/pos/basket/discount/  Body: {amount}."""

    def post(self, request):
        _, services, _, _basket_payload = _pos()
        from apps.pos.services import POSError

        sale = services.get_or_create_open_basket(request.user)
        try:
            services.set_discount(sale, request.data.get('amount'))
        except POSError as e:
            return Response({'ok': False, 'error': str(e)}, status=400)
        return Response({'ok': True, 'basket': _basket_payload(sale)})


class MobilePOSBasketClearView(POSAPIView):
    """POST /api/mobile/v1/pos/basket/clear/"""

    def post(self, request):
        _, services, _, _basket_payload = _pos()
        sale = services.get_or_create_open_basket(request.user)
        if sale.is_draft:
            sale.items.all().delete()
            sale.discount = 0
            sale.save(update_fields=['discount', 'updated_at'])
            sale.recalc_totals()
        return Response({'ok': True, 'basket': _basket_payload(sale)})


# ─────────────────────────────────────────────────────────────────────────────
# Completion + receipt
# ─────────────────────────────────────────────────────────────────────────────

class MobilePOSCompleteView(POSAPIView):
    """
    POST /api/mobile/v1/pos/complete/
    Body: {payment_method, amount_tendered?, payment_ref?,
           customer_name?, customer_phone?, customer_email?}

    Same semantics as the web till: atomic stock deduction via SALE
    ledger movements, race-safe receipt number, cash change calculation,
    automatic PDF email when customer_email is given. The response also
    carries the PUBLIC receipt URL so the app can share it straight to
    WhatsApp/SMS.
    """

    def post(self, request):
        _, services, _, _ = _pos()
        from apps.pos.services import POSError

        data = request.data or {}
        sale = services.get_or_create_open_basket(request.user)
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
            return Response({'ok': False, 'error': str(e)}, status=400)
        except Exception:
            logger.exception('mobile POS: complete_sale crashed')
            return Response(
                {'ok': False,
                 'error': 'Something went wrong completing the sale.'},
                status=500)

        return Response({
            'ok': True,
            'sale_id': str(sale.pk),
            'sale_no': sale.sale_no,
            'total': str(sale.total),
            'currency': sale.currency,
            'change_due': str(sale.change_due) if sale.change_due is not None else None,
            'emailed': bool(sale.customer_email),
            # Shareable no-login digital receipt (QR target) — perfect for
            # WhatsApp: the customer taps and sees their receipt.
            'receipt_public_url': services.public_receipt_url(sale, request=request),
            'receipt_pdf_url': _abs(request, reverse('pos_receipt_pdf',
                                                     kwargs={'pk': sale.pk})),
        }, status=201)


class MobilePOSReceiptEmailView(POSAPIView):
    """
    POST /api/mobile/v1/pos/receipt/<sale_id>/email/
    Body: {email?} — override / late-add the address, then (re)send.
    """

    def post(self, request, sale_id):
        _, services, _, _ = _pos()
        from apps.pos.models import POSSale

        sale = get_object_or_404(POSSale, pk=sale_id)
        new_email = (request.data.get('email') or '').strip()
        if new_email:
            sale.customer_email = new_email[:254]
            sale.customer = services.match_existing_customer(
                sale.customer_phone, sale.customer_email) or sale.customer
            sale.save(update_fields=['customer_email', 'customer', 'updated_at'])
        if not sale.customer_email:
            return Response({'ok': False,
                             'error': 'No email address on this sale.'},
                            status=400)
        sent = services.email_receipt(sale)
        return Response({'ok': sent,
                         'error': '' if sent else 'Email failed.'})


# ─────────────────────────────────────────────────────────────────────────────
# Day summary
# ─────────────────────────────────────────────────────────────────────────────

class MobilePOSSalesTodayView(POSAPIView):
    """
    GET /api/mobile/v1/pos/sales/today/

    The signed-in rep's sales today plus their running totals — the
    pocket version of the Z-summary. ?all=1 shows every cashier's sales
    (supervisors reviewing the day).
    """

    def get(self, request):
        from django.db.models import Count, Sum
        from apps.pos.models import POSSale
        from apps.pos.views import can_void_sale

        today = timezone.localdate()
        qs = (POSSale.objects
              .filter(completed_at__date=today)
              .select_related('cashier'))

        show_all = request.query_params.get('all') == '1' and can_void_sale(request.user)
        if not show_all:
            qs = qs.filter(cashier=request.user)

        completed = qs.filter(status=POSSale.Status.COMPLETED)
        agg = completed.aggregate(count=Count('id'), gross=Sum('total'))
        by_method = list(
            completed.values('payment_method')
                     .annotate(count=Count('id'), total=Sum('total'))
                     .order_by('-total'))

        sales = [{
            'id': str(s.pk),
            'sale_no': s.sale_no,
            'time': timezone.localtime(s.completed_at).strftime('%H:%M')
                    if s.completed_at else '',
            'cashier': (s.cashier.get_full_name()
                        or s.cashier.username) if s.cashier_id else '',
            'customer_name': s.customer_name,
            'total': str(s.total),
            'currency': s.currency,
            'payment_method': s.payment_method,
            'status': s.status,
        } for s in qs.order_by('-completed_at')[:100]]

        return Response({
            'ok': True,
            'date': today.isoformat(),
            'scope': 'all' if show_all else 'mine',
            'summary': {
                'count': agg['count'] or 0,
                'gross': str(agg['gross'] or '0.00'),
                'by_method': [
                    {'method': m['payment_method'],
                     'count': m['count'],
                     'total': str(m['total'])}
                    for m in by_method
                ],
            },
            'sales': sales,
        })
