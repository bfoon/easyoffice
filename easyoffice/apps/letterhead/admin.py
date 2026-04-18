"""
apps/letterhead/admin.py

Registers LetterheadTemplate in the Django admin so superusers can
manage designs without going through the builder UI.
"""
from django.contrib import admin
from django.utils.html import format_html

from .models import LetterheadTemplate


@admin.register(LetterheadTemplate)
class LetterheadTemplateAdmin(admin.ModelAdmin):
    list_display  = ('name', 'template_key', 'company_name', 'colour_swatch', 'is_active', 'updated_at')
    list_filter   = ('template_key', 'is_active')
    search_fields = ('name', 'company_name')
    readonly_fields = ('id', 'created_at', 'updated_at', 'created_by', 'last_edited_by')

    fieldsets = (
        (None, {
            'fields': ('id', 'name', 'is_active', 'template_key'),
        }),
        ('Company', {
            'fields': ('company_name', 'tagline', 'address', 'contact_info', 'logo'),
        }),
        ('Branding', {
            'fields': ('color_primary', 'color_secondary', 'font_header', 'font_body'),
        }),
        ('Audit', {
            'fields': ('created_by', 'last_edited_by', 'created_at', 'updated_at'),
            'classes': ('collapse',),
        }),
    )

    def colour_swatch(self, obj):
        return format_html(
            '<span style="display:inline-block;width:14px;height:14px;'
            'border-radius:3px;background:{};vertical-align:middle;'
            'margin-right:4px;border:1px solid #ccc"></span>'
            '<span style="display:inline-block;width:14px;height:14px;'
            'border-radius:3px;background:{};vertical-align:middle;'
            'border:1px solid #ccc"></span>',
            obj.color_primary,
            obj.color_secondary,
        )
    colour_swatch.short_description = 'Colours'

    actions = ['make_active']

    @admin.action(description='Set selected template as active')
    def make_active(self, request, queryset):
        if queryset.count() != 1:
            self.message_user(request, 'Select exactly one template to make active.', level='warning')
            return
        queryset.first().set_active()
        self.message_user(request, 'Letterhead set as active.')
