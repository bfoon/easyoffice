from django.contrib import admin

from apps.logistics.models import (
    Vehicle, Driver, DriverPortalToken, DriverTrustedDevice, DriverOTP,
    Shipment, ShipmentItem, ShipmentEvent, LocationPing, ProofOfDelivery,
)


@admin.register(Vehicle)
class VehicleAdmin(admin.ModelAdmin):
    list_display = ('label', 'ownership', 'status', 'registration', 'hire_vendor')
    list_filter = ('ownership', 'status')
    search_fields = ('label', 'registration', 'hire_vendor')


@admin.register(Driver)
class DriverAdmin(admin.ModelAdmin):
    list_display = ('full_name', 'phone', 'email', 'is_external', 'is_active')
    list_filter = ('is_external', 'is_active')
    search_fields = ('full_name', 'phone', 'email')


@admin.register(DriverPortalToken)
class DriverPortalTokenAdmin(admin.ModelAdmin):
    list_display = ('driver', 'is_active', 'is_email_bound', 'last_used_at')
    list_filter = ('is_active',)
    search_fields = ('driver__full_name', 'bound_email')
    readonly_fields = ('token',)


class ShipmentItemInline(admin.TabularInline):
    model = ShipmentItem
    extra = 0


class ShipmentEventInline(admin.TabularInline):
    model = ShipmentEvent
    extra = 0
    readonly_fields = ('status', 'note', 'actor', 'by_driver', 'created_at')


@admin.register(Shipment)
class ShipmentAdmin(admin.ModelAdmin):
    list_display = ('reference', 'customer_name', 'status', 'driver', 'vehicle',
                    'dispatched_at', 'delivered_at')
    list_filter = ('status',)
    search_fields = ('reference', 'customer_name', 'contact_name')
    inlines = [ShipmentItemInline, ShipmentEventInline]
    autocomplete_fields = ('driver', 'vehicle')


@admin.register(ProofOfDelivery)
class ProofOfDeliveryAdmin(admin.ModelAdmin):
    list_display = ('shipment', 'received_by_name', 'signed_at')
    search_fields = ('shipment__reference', 'received_by_name')


admin.site.register(DriverTrustedDevice)
admin.site.register(DriverOTP)
admin.site.register(LocationPing)
