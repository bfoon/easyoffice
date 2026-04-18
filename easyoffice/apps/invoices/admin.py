from django.contrib import admin
from .models import InvoiceDocument, InvoiceLineItem, InvoiceCounter, InvoiceTemplate


class InvoiceLineItemInline(admin.TabularInline):
    model = InvoiceLineItem
    extra = 0
    fields = ('position', 'description', 'quantity', 'unit_price', 'line_total')
    readonly_fields = ('line_total',)


@admin.register(InvoiceDocument)
class InvoiceDocumentAdmin(admin.ModelAdmin):
    list_display   = ('number', 'doc_type', 'status', 'client_name', 'total', 'currency',
                      'invoice_date', 'template', 'created_by', 'created_at')
    list_filter    = ('doc_type', 'status', 'currency', 'year', 'template')
    search_fields  = ('number', 'client_name', 'po_reference', 'client_email')
    readonly_fields = ('id', 'number', 'sequence', 'year',
                       'subtotal', 'tax_amount', 'total',
                       'finalized_at', 'voided_at', 'voided_by',
                       'generated_pdf', 'created_at', 'updated_at')
    inlines = [InvoiceLineItemInline]
    autocomplete_fields = ('letterhead', 'created_by', 'template')


@admin.register(InvoiceCounter)
class InvoiceCounterAdmin(admin.ModelAdmin):
    list_display = ('doc_type', 'year', 'last_sequence', 'updated_at')
    list_filter  = ('doc_type', 'year')


@admin.register(InvoiceLineItem)
class InvoiceLineItemAdmin(admin.ModelAdmin):
    list_display  = ('invoice', 'position', 'description', 'quantity', 'unit_price', 'line_total')
    search_fields = ('description', 'invoice__number')


@admin.register(InvoiceTemplate)
class InvoiceTemplateAdmin(admin.ModelAdmin):
    list_display  = ('name', 'visibility', 'owner', 'letterhead',
                     'locked_layout', 'doc_type', 'usage_count', 'updated_at')
    list_filter   = ('visibility', 'locked_layout', 'doc_type')
    search_fields = ('name', 'description', 'owner__first_name', 'owner__last_name')
    autocomplete_fields = ('letterhead', 'owner')
    readonly_fields = ('id', 'usage_count', 'created_at', 'updated_at')