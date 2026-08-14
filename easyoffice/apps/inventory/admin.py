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


from .models import InventoryAccessGrant


@admin.register(InventoryAccessGrant)
class InventoryAccessGrantAdmin(admin.ModelAdmin):
    list_display = ('scope_label', 'module', 'level', 'is_active',
                    'expires_at', 'granted_by', 'granted_at')
    list_filter  = ('is_active', 'module', 'level')
    search_fields = ('user__username', 'user__email', 'department__name', 'notes')
    readonly_fields = ('granted_at', 'updated_at')
    autocomplete_fields = ('user',)


# ════════════════════════════════════════════════════════════════════════════
# Licences
# ════════════════════════════════════════════════════════════════════════════

from .license_models import (
    License, LicenseEvent, LicenseRenewal, LicenseSeat, LicenseType,
)


@admin.register(LicenseType)
class LicenseTypeAdmin(admin.ModelAdmin):
    list_display  = ('code', 'name', 'vendor', 'billing_model',
                     'default_term_months', 'default_unit_cost',
                     'default_unit_price', 'is_active')
    list_filter   = ('billing_model', 'billing_cycle', 'is_active', 'vendor')
    search_fields = ('code', 'name', 'vendor__name')


class LicenseSeatInline(admin.TabularInline):
    model = LicenseSeat
    extra = 0
    fields = ('user', 'person_name', 'person_email', 'device_label',
              'assigned_at', 'released_at', 'release_reason')
    readonly_fields = ('assigned_at',)
    autocomplete_fields = ('user',)


class LicenseRenewalInline(admin.TabularInline):
    model = LicenseRenewal
    extra = 0
    readonly_fields = ('renewed_at',)


@admin.register(License)
class LicenseAdmin(admin.ModelAdmin):
    list_display  = ('reference', 'name', 'holder_label', 'vendor', 'seats',
                     'seats_assigned', 'end_date', 'health_label', 'status')
    list_filter   = ('status', 'holder_kind', 'billing_model', 'auto_renew',
                     'alerts_enabled', 'is_perpetual', 'vendor')
    search_fields = ('reference', 'name', 'customer_name', 'account_email',
                     'license_key', 'notes')
    date_hierarchy = 'end_date'
    readonly_fields = ('reference', 'reminders_sent', 'last_reminder_at',
                       'expired_notified_at', 'created_at', 'updated_at')
    autocomplete_fields = ('holder_user', 'owner')
    inlines = [LicenseSeatInline, LicenseRenewalInline]
    fieldsets = (
        ('What', {
            'fields': ('reference', 'name', 'license_type', 'vendor', 'status',
                       'is_active'),
        }),
        ('Who for', {
            'fields': ('holder_kind', 'holder_user', 'customer_name',
                       'customer_email', 'customer_ref', 'department', 'owner'),
        }),
        ('Credentials', {
            'classes': ('collapse',),
            'fields': ('license_key', 'account_email', 'portal_url', 'attachment'),
        }),
        ('Commercials', {
            'fields': ('billing_model', 'billing_cycle', 'seats', 'unit_cost',
                       'unit_price', 'setup_cost', 'setup_price', 'currency',
                       'purchase_ref'),
        }),
        ('Term', {
            'fields': ('start_date', 'end_date', 'is_perpetual', 'auto_renew',
                       'renewal_term_months', 'grace_days'),
        }),
        ('Alerts', {
            'fields': ('alerts_enabled', 'reminder_days', 'notify_customer',
                       'reminders_sent', 'last_reminder_at', 'expired_notified_at'),
        }),
        ('Meta', {'fields': ('notes', 'created_by', 'created_at', 'updated_at')}),
    )

    @admin.display(description='Held by')
    def holder_label(self, obj):
        return obj.holder_label

    @admin.display(description='In use')
    def seats_assigned(self, obj):
        return obj.seats_assigned

    @admin.display(description='Expiry')
    def health_label(self, obj):
        return obj.health_label


@admin.register(LicenseSeat)
class LicenseSeatAdmin(admin.ModelAdmin):
    list_display  = ('license', 'holder_label', 'assigned_at', 'released_at')
    list_filter   = ('license',)
    search_fields = ('license__reference', 'license__name', 'person_name',
                     'person_email', 'device_label', 'user__username')
    autocomplete_fields = ('user', 'asset')

    @admin.display(description='Held by')
    def holder_label(self, obj):
        return obj.holder_label


@admin.register(LicenseEvent)
class LicenseEventAdmin(admin.ModelAdmin):
    list_display  = ('created_at', 'license', 'kind', 'actor', 'message')
    list_filter   = ('kind',)
    search_fields = ('license__reference', 'license__name', 'message')
    readonly_fields = ('created_at',)
    date_hierarchy = 'created_at'


@admin.register(LicenseRenewal)
class LicenseRenewalAdmin(admin.ModelAdmin):
    list_display  = ('license', 'previous_end', 'new_end', 'term_months',
                     'seats', 'total_cost', 'renewed_by', 'renewed_at')
    search_fields = ('license__reference', 'license__name', 'invoice_ref')
    date_hierarchy = 'renewed_at'
