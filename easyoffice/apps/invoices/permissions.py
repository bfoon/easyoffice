"""
Permission helpers for the invoices app.

Access is granted to:
    - superusers
    - users in any of the groups: CEO, Sales, HR, Admin
"""
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied

# Group names that unlock invoice access (case-insensitive match)
INVOICE_ACCESS_GROUPS = {'CEO', 'Sales', 'HR', 'Admin'}


def can_use_invoices(user):
    """Return True if `user` may access the invoice module."""
    if not user or not user.is_authenticated:
        return False
    if user.is_superuser:
        return True
    if user.groups.filter(name__in=INVOICE_ACCESS_GROUPS).exists():
        return True
    # case-insensitive fallback for typos like 'sales' vs 'Sales'
    user_groups_lower = {g.lower() for g in user.groups.values_list('name', flat=True)}
    return any(g.lower() in user_groups_lower for g in INVOICE_ACCESS_GROUPS)


class InvoiceAccessMixin(LoginRequiredMixin):
    """
    Mixin enforcing that only CEO / Sales / HR / Admin / superuser can access the view.
    Use on every view in the invoice app.
    """
    def dispatch(self, request, *args, **kwargs):
        if not can_use_invoices(request.user):
            raise PermissionDenied("You don't have access to the Invoice module.")
        return super().dispatch(request, *args, **kwargs)
