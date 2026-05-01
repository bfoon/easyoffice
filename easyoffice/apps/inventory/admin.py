"""apps/inventory/admin.py — Django admin registrations."""
from django.contrib import admin

from .models import (
    Asset, AssetAssignment, AssetEvent, AssetMaintenance, Category,
    Location, Product, StockBatch, StockItem, StockMovement, StockRequest,
    StockRequestLine, StockTake, StockTakeLine, Supplier,
)
from .api.models import ApiKey


@admin.register(Location)
class LocationAdmin(admin.ModelAdmin):
    list_display  = ('code', 'name', 'kind', 'is_sellable', 'is_active')
    list_filter   = ('kind', 'is_sellable', 'is_active')
    search_fields = ('code', 'name', 'qr_token')


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display  = ('name', 'parent', 'icon')
    list_filter   = ('parent',)
    search_fields = ('name',)


@admin.register(Supplier)
class SupplierAdmin(admin.ModelAdmin):
    list_display  = ('name', 'phone', 'email', 'is_active')
    list_filter   = ('is_active',)
    search_fields = ('name', 'phone', 'email')


class StockItemInline(admin.TabularInline):
    model = StockItem
    extra = 0
    readonly_fields = ('qr_token',)


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display    = ('sku', 'name', 'kind', 'category', 'sell_price', 'cost_price', 'is_sellable', 'is_active')
    list_filter     = ('kind', 'is_sellable', 'is_purchasable', 'is_for_internal', 'is_active')
    search_fields   = ('sku', 'name', 'barcode', 'qr_token')
    readonly_fields = ('qr_token',)
    inlines         = [StockItemInline]


@admin.register(StockItem)
class StockItemAdmin(admin.ModelAdmin):
    list_display  = ('product', 'location', 'quantity', 'reserved_quantity', 'bin_code')
    list_filter   = ('location',)
    search_fields = ('product__sku', 'product__name', 'location__code', 'qr_token')


@admin.register(StockBatch)
class StockBatchAdmin(admin.ModelAdmin):
    list_display = ('batch_no', 'stock_item', 'quantity', 'expiry_date', 'received_at')
    search_fields = ('batch_no',)


@admin.register(StockMovement)
class StockMovementAdmin(admin.ModelAdmin):
    list_display = ('created_at', 'kind', 'product', 'location', 'quantity', 'actor')
    list_filter  = ('kind', 'location')
    search_fields = ('product__sku', 'product__name', 'source_ref', 'notes')
    readonly_fields = ('created_at',)
    date_hierarchy = 'created_at'


class StockTakeLineInline(admin.TabularInline):
    model = StockTakeLine
    extra = 0


@admin.register(StockTake)
class StockTakeAdmin(admin.ModelAdmin):
    list_display = ('reference', 'location', 'status', 'started_at', 'finalised_at')
    list_filter  = ('status', 'location')
    inlines = [StockTakeLineInline]


class AssetAssignmentInline(admin.TabularInline):
    model = AssetAssignment
    extra = 0
    readonly_fields = ('assigned_at', 'returned_at')


class AssetMaintenanceInline(admin.TabularInline):
    model = AssetMaintenance
    extra = 0


@admin.register(Asset)
class AssetAdmin(admin.ModelAdmin):
    list_display    = ('tag', 'name', 'category', 'status', 'condition', 'location', 'purchase_cost')
    list_filter     = ('status', 'condition', 'category', 'location')
    search_fields   = ('tag', 'name', 'serial_number', 'qr_token')
    readonly_fields = ('qr_token',)
    inlines         = [AssetAssignmentInline, AssetMaintenanceInline]


@admin.register(AssetEvent)
class AssetEventAdmin(admin.ModelAdmin):
    list_display = ('created_at', 'asset', 'kind', 'actor', 'message')
    list_filter  = ('kind',)
    readonly_fields = ('created_at',)
    date_hierarchy = 'created_at'


class StockRequestLineInline(admin.TabularInline):
    model = StockRequestLine
    extra = 0


@admin.register(StockRequest)
class StockRequestAdmin(admin.ModelAdmin):
    list_display = ('reference', 'requested_by', 'status', 'created_at')
    list_filter  = ('status',)
    inlines      = [StockRequestLineInline]


@admin.register(ApiKey)
class ApiKeyAdmin(admin.ModelAdmin):
    list_display    = ('name', 'user', 'lookup_prefix', 'is_active', 'expires_at', 'last_used_at', 'created_at')
    list_filter     = ('is_active',)
    readonly_fields = ('key_hash', 'lookup_prefix', 'last_used_at', 'created_at')
    search_fields   = ('name', 'user__username', 'user__email', 'lookup_prefix')

    def has_add_permission(self, request):
        # Force use of the management command — never let admins add raw keys.
        return False