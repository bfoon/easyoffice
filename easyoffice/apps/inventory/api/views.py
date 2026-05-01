"""
apps/inventory/api/views.py
───────────────────────────
Public API for the inventory module.

Endpoints:

    GET  /api/inventory/products/                — list products
    GET  /api/inventory/products/<id>/           — product detail (incl. stock)
    GET  /api/inventory/locations/               — list locations
    GET  /api/inventory/suppliers/               — list suppliers
    GET  /api/inventory/assets/                  — list assets
    GET  /api/inventory/assets/<id>/             — asset detail
    POST /api/inventory/assets/<id>/assign/      — assign asset to a user
    POST /api/inventory/movements/               — create a stock movement
    GET  /api/inventory/movements/               — list recent movements
    POST /api/inventory/qr/resolve/              — resolve a QR token
                                                   (body: {"token": "eo-..."})

Authentication accepts ANY of:
    - Api-Key header (this module's ApiKey)
    - DRF Token (rest_framework.authtoken)
    - JWT (rest_framework_simplejwt) if installed
"""
from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation

from django.contrib.auth import get_user_model
from django.shortcuts import get_object_or_404
from rest_framework import status, viewsets
from rest_framework.authentication import (
    SessionAuthentication, TokenAuthentication,
)
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .. import services
from ..models import (
    Asset, Location, Product, StockItem, StockMovement, Supplier,
)
from ..permissions import can_manage_stock, can_use_inventory
from .authentication import ApiKeyAuthentication
from .serializers import (
    AssetSerializer, LocationLiteSerializer, ProductLiteSerializer,
    ProductSerializer, QRScanResponseSerializer, StockMovementCreateSerializer,
    StockMovementSerializer, SupplierLiteSerializer,
)


logger = logging.getLogger(__name__)
User = get_user_model()


# Optional JWT — only attach if installed
try:
    from rest_framework_simplejwt.authentication import JWTAuthentication  # type: ignore
    _AUTH_CLASSES = [ApiKeyAuthentication, JWTAuthentication, TokenAuthentication, SessionAuthentication]
except Exception:  # pragma: no cover
    _AUTH_CLASSES = [ApiKeyAuthentication, TokenAuthentication, SessionAuthentication]


class _BaseInventoryViewSet(viewsets.ReadOnlyModelViewSet):
    authentication_classes = _AUTH_CLASSES
    permission_classes = [IsAuthenticated]


class ProductViewSet(_BaseInventoryViewSet):
    queryset = Product.objects.select_related('category', 'preferred_supplier').prefetch_related('stock_items__location')
    serializer_class = ProductSerializer

    def get_serializer_class(self):
        if self.action == 'list':
            return ProductLiteSerializer
        return ProductSerializer

    def get_queryset(self):
        qs = super().get_queryset()
        params = self.request.query_params

        if q := params.get('q'):
            from django.db.models import Q as DQ
            qs = qs.filter(DQ(name__icontains=q) | DQ(sku__icontains=q) | DQ(barcode__icontains=q))
        if kind := params.get('kind'):
            qs = qs.filter(kind=kind)
        if cat := params.get('category'):
            qs = qs.filter(category_id=cat)
        if params.get('sellable_only'):
            qs = qs.filter(is_sellable=True, is_active=True)
        if params.get('low_stock'):
            # Server-side filter on low stock — pull recent batch and filter in Python
            ids = [p.id for p in qs[:500] if p.is_low_stock]
            qs = qs.filter(id__in=ids)
        return qs.order_by('name')


class LocationViewSet(_BaseInventoryViewSet):
    queryset = Location.objects.filter(is_active=True).select_related('department')
    serializer_class = LocationLiteSerializer


class SupplierViewSet(_BaseInventoryViewSet):
    queryset = Supplier.objects.filter(is_active=True)
    serializer_class = SupplierLiteSerializer


class AssetViewSet(_BaseInventoryViewSet):
    queryset = Asset.objects.select_related('category', 'location', 'supplier').prefetch_related('assignments__assignee')
    serializer_class = AssetSerializer

    def get_queryset(self):
        qs = super().get_queryset()
        params = self.request.query_params
        if q := params.get('q'):
            from django.db.models import Q as DQ
            qs = qs.filter(DQ(name__icontains=q) | DQ(tag__icontains=q) | DQ(serial_no__icontains=q))
        if s := params.get('status'):
            qs = qs.filter(status=s)
        if loc := params.get('location'):
            qs = qs.filter(location_id=loc)
        return qs.order_by('-created_at')

    @action(detail=True, methods=['post'])
    def assign(self, request, pk=None):
        if not can_manage_stock(request.user):
            return Response({'detail': 'Permission denied.'}, status=status.HTTP_403_FORBIDDEN)
        asset = self.get_object()
        assignee_id = request.data.get('assignee_id')
        if not assignee_id:
            return Response({'detail': 'assignee_id is required.'}, status=400)
        try:
            assignee = User.objects.get(pk=assignee_id, is_active=True)
        except (User.DoesNotExist, ValueError):
            return Response({'detail': 'assignee not found.'}, status=404)
        services.assign_asset(
            asset, assignee=assignee, actor=request.user,
            purpose=request.data.get('purpose', '') or '',
        )
        asset.refresh_from_db()
        return Response(self.get_serializer(asset).data, status=200)

    @action(detail=True, methods=['post'])
    def return_asset(self, request, pk=None):
        if not can_manage_stock(request.user):
            return Response({'detail': 'Permission denied.'}, status=status.HTTP_403_FORBIDDEN)
        asset = self.get_object()
        services.return_asset(
            asset, actor=request.user,
            condition=request.data.get('condition', '') or '',
            note=request.data.get('note', '') or '',
        )
        asset.refresh_from_db()
        return Response(self.get_serializer(asset).data, status=200)


class StockMovementViewSet(viewsets.ModelViewSet):
    """
    list/retrieve are read-only views over the ledger.
    create delegates to services.* — never writes the model directly.
    """
    queryset = StockMovement.objects.select_related('product', 'location', 'created_by').order_by('-created_at')
    authentication_classes = _AUTH_CLASSES
    permission_classes = [IsAuthenticated]
    http_method_names = ['get', 'post', 'head', 'options']

    def get_serializer_class(self):
        if self.action == 'create':
            return StockMovementCreateSerializer
        return StockMovementSerializer

    def get_queryset(self):
        qs = super().get_queryset()
        params = self.request.query_params
        if p := params.get('product'):
            qs = qs.filter(product_id=p)
        if loc := params.get('location'):
            qs = qs.filter(location_id=loc)
        if k := params.get('kind'):
            qs = qs.filter(kind=k)
        return qs[:500]

    def create(self, request, *args, **kwargs):
        if not can_manage_stock(request.user):
            return Response({'detail': 'Permission denied.'}, status=status.HTTP_403_FORBIDDEN)

        ser = self.get_serializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data

        try:
            product  = Product.objects.get(pk=data['product_id'])
            location = Location.objects.get(pk=data['location_id'])
        except (Product.DoesNotExist, Location.DoesNotExist):
            return Response({'detail': 'product_id or location_id not found.'}, status=404)

        kind = data['kind']
        qty = Decimal(str(data['quantity']))

        try:
            if kind == StockMovement.Kind.RECEIVE:
                supplier = None
                if sup_id := data.get('supplier_id'):
                    try:
                        supplier = Supplier.objects.get(pk=sup_id)
                    except Supplier.DoesNotExist:
                        pass
                mvmt = services.receive_stock(
                    product=product, location=location, quantity=qty,
                    cost_price=data.get('cost_price'),
                    batch_no=data.get('batch_no', '') or '',
                    expiry_date=data.get('expiry_date'),
                    supplier=supplier,
                    source_kind=data.get('source_kind', '') or '',
                    source_ref=data.get('source_ref', '') or '',
                    actor=request.user,
                    notes=data.get('notes', '') or '',
                )
            elif kind in {
                StockMovement.Kind.SALE,
                StockMovement.Kind.INTERNAL_USE,
                StockMovement.Kind.RETURN_OUT,
            }:
                mvmt = services.issue_stock(
                    product=product, location=location, quantity=qty,
                    kind=kind,
                    source_kind=data.get('source_kind', '') or '',
                    source_ref=data.get('source_ref', '') or '',
                    actor=request.user,
                    notes=data.get('notes', '') or '',
                )
            elif kind in {StockMovement.Kind.ADJUST_IN, StockMovement.Kind.ADJUST_OUT}:
                mvmt = services.adjust_stock(
                    product=product, location=location, quantity=qty,
                    kind=kind, actor=request.user,
                    notes=data.get('notes', '') or '',
                )
            elif kind == StockMovement.Kind.SCRAP:
                mvmt = services.scrap_stock(
                    product=product, location=location, quantity=qty,
                    actor=request.user, reason=data.get('notes', '') or '',
                )
            else:
                return Response(
                    {'detail': f'Movement kind "{kind}" is not allowed via this endpoint. '
                               f'Use the dedicated transfer endpoint for transfers.'},
                    status=400,
                )
        except ValueError as e:
            return Response({'detail': str(e)}, status=400)
        except Exception:
            logger.exception('Failed to create stock movement via API')
            return Response({'detail': 'Internal error processing movement.'}, status=500)

        out = StockMovementSerializer(mvmt).data
        return Response(out, status=status.HTTP_201_CREATED)


class QRResolveAPIView(APIView):
    """
    POST {"token": "eo-..."}
    Returns: {"kind": "asset"|"product"|"stockitem"|"location",
              "object": {...}, "redirect_url": "..."}
    """
    authentication_classes = _AUTH_CLASSES
    permission_classes = [IsAuthenticated]

    def post(self, request):
        if not can_use_inventory(request.user):
            return Response({'detail': 'Permission denied.'}, status=403)

        token = (request.data.get('token') or '').strip()
        if not token:
            return Response({'detail': 'token is required.'}, status=400)

        from django.urls import reverse

        # Asset
        if asset := Asset.objects.filter(qr_token=token).first():
            return Response(QRScanResponseSerializer({
                'kind': 'asset',
                'object': AssetSerializer(asset).data,
                'redirect_url': reverse('inventory:asset_detail', args=[asset.pk]),
            }).data)

        # Product
        if product := Product.objects.filter(qr_token=token).first():
            return Response(QRScanResponseSerializer({
                'kind': 'product',
                'object': ProductSerializer(product).data,
                'redirect_url': reverse('inventory:product_detail', args=[product.pk]),
            }).data)

        # StockItem
        if si := StockItem.objects.filter(qr_token=token).select_related('product', 'location').first():
            return Response(QRScanResponseSerializer({
                'kind': 'stockitem',
                'object': {
                    'id': str(si.pk),
                    'product': str(si.product.pk),
                    'product_name': si.product.name,
                    'location': str(si.location.pk),
                    'location_code': si.location.code,
                    'quantity': str(si.quantity),
                    'available': str(si.available),
                },
                'redirect_url': reverse('inventory:product_detail', args=[si.product.pk]),
            }).data)

        # Location
        if location := Location.objects.filter(qr_token=token).first():
            return Response(QRScanResponseSerializer({
                'kind': 'location',
                'object': LocationLiteSerializer(location).data,
                'redirect_url': reverse('inventory:location_detail', args=[location.pk]),
            }).data)

        return Response({'detail': 'Token not recognised.'}, status=404)
