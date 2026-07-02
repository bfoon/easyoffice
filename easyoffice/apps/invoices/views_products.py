"""
apps/invoices/views_products.py
================================

Bridge endpoints between the invoice builder and the inventory app.

Two JSON APIs, both gated by InvoiceAccessMixin (CEO / Sales / HR / Admin /
superuser). The inventory app is imported lazily so the invoice module keeps
working even if apps.inventory is not installed — the autocomplete simply
returns no results and hides the "save to inventory" option.

  GET  /invoices/api/products/search/?q=<text>
        → { ok, results: [...], can_add, inventory_available }

  POST /invoices/api/products/quick-add/   (JSON body)
        { name, unit_price, currency, description? }
        → { ok, created, product: {...} }
        403 if the user lacks inventory manage permission.

Wire-up: import in apps/invoices/urls.py:

    from . import views_products
    ...
    path('api/products/search/',    views_products.ProductSearchAPIView.as_view(),   name='product_search_api'),
    path('api/products/quick-add/', views_products.ProductQuickAddAPIView.as_view(), name='product_quick_add_api'),
"""
import json
import logging
import secrets
from decimal import Decimal, InvalidOperation

from django.db.models import Q
from django.http import JsonResponse
from django.views import View

from .permissions import InvoiceAccessMixin

log = logging.getLogger(__name__)

SEARCH_LIMIT = 12


# ─────────────────────────────────────────────────────────────────────────────
# Lazy inventory imports (avoid hard dependency + circular imports)
# ─────────────────────────────────────────────────────────────────────────────

def _get_product_model():
    """Return the inventory Product model, or None if unavailable."""
    try:
        from apps.inventory.models import Product  # noqa: WPS433
        return Product
    except Exception:  # app not installed / import error
        return None


def _user_can_add_products(user) -> bool:
    """True if `user` may create inventory products (stock manager)."""
    try:
        from apps.inventory.permissions import can_manage_stock  # noqa: WPS433
        return bool(can_manage_stock(user))
    except Exception:
        return False


def _serialize_product(p) -> dict:
    """Compact JSON payload for the builder autocomplete."""
    try:
        stock = float(p.total_available)
    except Exception:
        stock = None
    return {
        'id':           str(p.pk),
        'sku':          p.sku,
        'name':         p.name,
        'description':  (p.description or '')[:200],
        'unit_price':   str(p.sell_price or '0.00'),
        'currency':     p.currency,
        'unit_label':   p.unit_label,
        'kind':         p.kind,
        'stock':        stock,
        'stock_status': getattr(p, 'stock_status', 'na'),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Search
# ─────────────────────────────────────────────────────────────────────────────

class ProductSearchAPIView(InvoiceAccessMixin, View):
    """
    Autocomplete search over sellable inventory products.
    Matches name, SKU and barcode (icontains, so partial spelling works).
    """

    def get(self, request):
        q = (request.GET.get('q') or '').strip()
        Product = _get_product_model()
        can_add = _user_can_add_products(request.user)

        if Product is None:
            return JsonResponse({
                'ok': True, 'results': [],
                'can_add': False, 'inventory_available': False,
            })

        results = []
        if len(q) >= 1:
            qs = (
                Product.objects
                .filter(is_active=True, is_sellable=True)
                .filter(
                    Q(name__icontains=q) |
                    Q(sku__icontains=q) |
                    Q(barcode__icontains=q)
                )
                .order_by('name')[:SEARCH_LIMIT]
            )
            results = [_serialize_product(p) for p in qs]

        return JsonResponse({
            'ok': True,
            'results': results,
            'can_add': can_add,
            'inventory_available': True,
        })


# ─────────────────────────────────────────────────────────────────────────────
# Quick add
# ─────────────────────────────────────────────────────────────────────────────

def _generate_sku(Product, name: str) -> str:
    """
    Generate a unique SKU from the item name:
      'Adhesive Tape 50mm' → 'ADH-4821' (retries on collision).
    """
    letters = ''.join(ch for ch in name.upper() if ch.isalnum())[:3] or 'PRD'
    for _ in range(20):
        candidate = f'{letters}-{secrets.randbelow(9000) + 1000}'
        if not Product.objects.filter(sku=candidate).exists():
            return candidate
    # Extremely unlikely fallback — widen the numeric space
    return f'{letters}-{secrets.randbelow(900000) + 100000}'


class ProductQuickAddAPIView(InvoiceAccessMixin, View):
    """
    Create a new inventory Product from an invoice line item.

    Only users with inventory manage permission (can_manage_stock) may call
    this — everyone else gets a 403 and the invoice simply keeps the line
    as free text (which is always allowed).
    """

    def post(self, request):
        Product = _get_product_model()
        if Product is None:
            return JsonResponse(
                {'ok': False, 'error': 'Inventory module is not available.'},
                status=400,
            )

        if not _user_can_add_products(request.user):
            return JsonResponse(
                {'ok': False,
                 'error': "You don't have permission to add products to the inventory."},
                status=403,
            )

        try:
            payload = json.loads(request.body.decode('utf-8'))
        except (ValueError, UnicodeDecodeError):
            return JsonResponse({'ok': False, 'error': 'Invalid JSON body.'}, status=400)

        name = (payload.get('name') or '').strip()
        if not name:
            return JsonResponse({'ok': False, 'error': 'Item name is required.'}, status=400)
        if len(name) > 200:
            name = name[:200]

        # Duplicate guard — if it already exists, just hand it back.
        existing = Product.objects.filter(name__iexact=name, is_active=True).first()
        if existing:
            return JsonResponse({
                'ok': True, 'created': False,
                'message': f'"{existing.name}" already exists in the inventory ({existing.sku}).',
                'product': _serialize_product(existing),
            })

        try:
            sell_price = Decimal(str(payload.get('unit_price') or '0'))
            if sell_price < 0:
                sell_price = Decimal('0.00')
        except (InvalidOperation, TypeError):
            sell_price = Decimal('0.00')

        currency = (payload.get('currency') or 'GMD').strip().upper()[:3] or 'GMD'
        description = (payload.get('description') or '').strip()

        product = Product.objects.create(
            sku=_generate_sku(Product, name),
            name=name,
            description=description,
            kind=Product.Kind.STOCKED,
            sell_price=sell_price,
            currency=currency,
            is_sellable=True,
            created_by=request.user,
        )
        log.info(
            'Product %s (%s) quick-added from invoice builder by %s',
            product.sku, product.name, request.user,
        )
        return JsonResponse({
            'ok': True, 'created': True,
            'message': f'"{product.name}" saved to inventory as {product.sku}.',
            'product': _serialize_product(product),
        })
