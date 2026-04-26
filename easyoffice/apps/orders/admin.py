"""apps/orders/admin.py"""
from django.contrib import admin
from .models import SalesOrder, SalesOrderItem, OrderEvent
from .api.models import ApiKey


class SalesOrderItemInline(admin.TabularInline):
    model = SalesOrderItem
    extra = 0
    fields = ('position', 'description', 'quantity', 'unit_price')


@admin.register(SalesOrder)
class SalesOrderAdmin(admin.ModelAdmin):
    list_display  = ('order_no', 'source', 'status', 'customer', 'total', 'created_at')
    list_filter   = ('source', 'status', 'created_at')
    search_fields = ('order_no', 'contact_name', 'contact_phone', 'contact_email', 'external_ref')
    readonly_fields = ('order_no', 'subtotal', 'tax_amount', 'total', 'created_at', 'updated_at',
                       'fulfilled_at', 'proforma', 'invoice', 'delivery_note')
    inlines       = [SalesOrderItemInline]
    date_hierarchy = 'created_at'


@admin.register(OrderEvent)
class OrderEventAdmin(admin.ModelAdmin):
    list_display  = ('order', 'kind', 'actor', 'message', 'created_at')
    list_filter   = ('kind', 'created_at')
    search_fields = ('order__order_no', 'message')
    readonly_fields = ('id', 'order', 'kind', 'actor', 'message', 'payload', 'created_at')


@admin.register(ApiKey)
class ApiKeyAdmin(admin.ModelAdmin):
    """
    NOTE: keys are stored hashed, so the admin can't show the raw value.
    Issue new keys via the management command (or a future settings UI).
    """
    list_display  = ('name', 'key_prefix', 'user', 'is_active', 'last_used_at', 'created_at')
    list_filter   = ('is_active',)
    search_fields = ('name', 'user__email')
    readonly_fields = ('key_hash', 'key_prefix', 'created_at', 'last_used_at')
