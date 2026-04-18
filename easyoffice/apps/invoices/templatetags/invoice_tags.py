from django import template
from apps.invoices.permissions import can_use_invoices

register = template.Library()


@register.filter
def has_invoice_access(user):
    return can_use_invoices(user)
