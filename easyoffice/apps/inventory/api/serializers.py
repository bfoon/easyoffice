"""apps/inventory/api/serializers.py — DRF serializers for the public API."""
from decimal import Decimal

from rest_framework import serializers

from ..models import (
    Asset, AssetAssignment, Category, Location, Product, StockBatch,
    StockItem, StockMovement, Supplier,
)


# ── Lookups (small, embeddable) ────────────────────────────────────────────

class CategoryLiteSerializer(serializers.ModelSerializer):
    class Meta:
        model = Category
        fields = ['id', 'name']


class LocationLiteSerializer(serializers.ModelSerializer):
    class Meta:
        model = Location
        fields = ['id', 'code', 'name', 'kind', 'is_sellable']


class SupplierLiteSerializer(serializers.ModelSerializer):
    class Meta:
        model = Supplier
        fields = ['id', 'name']


# ── Stock ──────────────────────────────────────────────────────────────────

class StockItemSerializer(serializers.ModelSerializer):
    location  = LocationLiteSerializer(read_only=True)
    available = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True)

    class Meta:
        model  = StockItem
        fields = ['id', 'location', 'quantity', 'reserved_quantity',
                  'available', 'bin_code', 'qr_token', 'last_movement_at']


class StockMovementSerializer(serializers.ModelSerializer):
    location_code = serializers.CharField(source='location.code', read_only=True)
    product_sku   = serializers.CharField(source='product.sku', read_only=True)

    class Meta:
        model  = StockMovement
        fields = ['id', 'product_sku', 'location_code', 'kind', 'quantity',
                  'source_kind', 'source_ref', 'notes', 'occurred_at']
        read_only_fields = fields


class StockBatchSerializer(serializers.ModelSerializer):
    is_expired       = serializers.BooleanField(read_only=True)

    class Meta:
        model  = StockBatch
        fields = ['id', 'batch_no', 'quantity', 'received_at', 'expiry_date',
                  'cost_price', 'is_expired']


# ── Products ───────────────────────────────────────────────────────────────

class ProductSerializer(serializers.ModelSerializer):
    category           = CategoryLiteSerializer(read_only=True)
    preferred_supplier = SupplierLiteSerializer(read_only=True)
    stock_items        = StockItemSerializer(many=True, read_only=True)

    total_on_hand      = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True)
    total_available    = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True)
    is_low_stock       = serializers.BooleanField(read_only=True)
    stock_status       = serializers.CharField(read_only=True)

    class Meta:
        model  = Product
        fields = [
            'id', 'sku', 'barcode', 'name', 'description', 'kind',
            'category', 'unit_label', 'cost_price', 'sell_price', 'currency',
            'is_sellable', 'is_purchasable', 'is_for_internal',
            'track_serial', 'track_batch',
            'reorder_point', 'reorder_qty', 'preferred_supplier',
            'qr_token', 'is_active',
            'stock_items', 'total_on_hand', 'total_available',
            'is_low_stock', 'stock_status',
        ]
        read_only_fields = ['id', 'qr_token']


class ProductLiteSerializer(serializers.ModelSerializer):
    """For lists where we don't want to expand stock_items."""
    total_available = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True)

    class Meta:
        model  = Product
        fields = ['id', 'sku', 'barcode', 'name', 'kind', 'unit_label',
                  'sell_price', 'currency', 'is_sellable',
                  'total_available', 'qr_token']


# ── Stock movement create (POST) ──────────────────────────────────────────

class StockMovementCreateSerializer(serializers.Serializer):
    """
    Generic write endpoint that delegates to services.py, so the state
    rules (low-stock alerts, batch handling, reservations) all run.
    """
    product_id   = serializers.UUIDField()
    location_id  = serializers.UUIDField()
    quantity     = serializers.DecimalField(max_digits=14, decimal_places=2, min_value=Decimal('0.01'))
    kind         = serializers.ChoiceField(choices=StockMovement.Kind.choices)
    cost_price   = serializers.DecimalField(max_digits=12, decimal_places=2, required=False, min_value=Decimal('0'))
    batch_no     = serializers.CharField(required=False, allow_blank=True, max_length=50)
    expiry_date  = serializers.DateField(required=False, allow_null=True)
    supplier_id  = serializers.UUIDField(required=False, allow_null=True)
    source_kind  = serializers.CharField(required=False, allow_blank=True, max_length=30)
    source_ref   = serializers.CharField(required=False, allow_blank=True, max_length=80)
    notes        = serializers.CharField(required=False, allow_blank=True, max_length=300)


# ── Assets ─────────────────────────────────────────────────────────────────

class AssetAssignmentLiteSerializer(serializers.ModelSerializer):
    assignee_name = serializers.CharField(source='assignee.get_full_name', read_only=True)

    class Meta:
        model  = AssetAssignment
        fields = ['id', 'assignee_name', 'assigned_at', 'returned_at', 'purpose']


class AssetSerializer(serializers.ModelSerializer):
    location      = LocationLiteSerializer(read_only=True)
    category      = CategoryLiteSerializer(read_only=True)
    assignments   = AssetAssignmentLiteSerializer(many=True, read_only=True)
    book_value    = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True)
    is_under_warranty = serializers.BooleanField(read_only=True)
    assignee_name = serializers.SerializerMethodField()

    def get_assignee_name(self, obj):
        if obj.assigned_to:
            return obj.assigned_to.get_full_name() or obj.assigned_to.username
        return ''

    class Meta:
        model  = Asset
        fields = [
            'id', 'tag', 'name', 'description', 'category',
            'serial_number', 'model_number', 'manufacturer',
            'location', 'assignee_name',
            'status', 'condition',
            'purchase_date', 'purchase_cost', 'currency', 'warranty_until',
            'useful_life_months', 'salvage_value', 'book_value',
            'is_under_warranty', 'qr_token', 'is_active',
            'assignments',
        ]
        read_only_fields = ['id', 'qr_token']


# ── QR scan response ──────────────────────────────────────────────────────

class QRScanResponseSerializer(serializers.Serializer):
    """What a mobile scanner gets when it POSTs a token."""
    kind   = serializers.CharField()        # 'product' | 'asset' | 'location' | 'stockitem'
    object = serializers.JSONField()
    redirect_url = serializers.CharField(required=False, allow_blank=True)
