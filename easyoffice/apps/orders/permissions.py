"""
apps/orders/permissions.py
──────────────────────────
Who can do what with sales orders.

    • View / create  : Customer Service agents, Sales, supervisors, CEO, superusers
    • Fulfill        : Same set as above (per project decision: "CS agents fulfill,
                       supervisors can override")
    • Cancel         : Same — but a cancelled order cannot be un-cancelled

The helpers are duck-typed against User: any falsy / unauthenticated user
fails fast.
"""
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied


# Group names that grant access to the orders module. Match is
# case-insensitive (handled inside the helper).
_ORDERS_GROUPS = {
    'customer service', 'customer_service', 'cs',
    'sales',
    'supervisor', 'admin', 'administrator', 'manager', 'ceo',
}


def _has_orders_group(user):
    if not user.groups.exists():
        return False
    user_group_names = {g.lower() for g in user.groups.values_list('name', flat=True)}
    return bool(user_group_names & _ORDERS_GROUPS)


def _has_supervisor_title(user):
    """Match position title via StaffProfile, defensively (no profile = False)."""
    try:
        title = (user.staffprofile.position.title or '').lower()
    except Exception:
        return False
    return any(kw in title for kw in (
        'customer service', 'sales', 'supervisor', 'manager',
        'admin', 'ceo', 'head of',
    ))


def can_use_orders(user):
    """View, create, edit own draft orders."""
    if not user or not getattr(user, 'is_authenticated', False):
        return False
    if user.is_superuser or user.is_staff:
        return True
    return _has_orders_group(user) or _has_supervisor_title(user)


def can_fulfill_order(user, order=None):
    """
    Fulfillment generates real invoices. Same baseline as can_use_orders
    (per project decision); the `order` arg is accepted so we can tighten
    the rule later (e.g. only the assigned agent) without changing callers.
    """
    return can_use_orders(user)


def can_cancel_order(user, order=None):
    return can_use_orders(user)


def can_override(user):
    """Supervisor / admin override — used for re-opening or editing fulfilled."""
    if not user or not getattr(user, 'is_authenticated', False):
        return False
    if user.is_superuser or user.is_staff:
        return True
    user_group_names = {g.lower() for g in user.groups.values_list('name', flat=True)}
    return bool(user_group_names & {'supervisor', 'admin', 'administrator', 'manager', 'ceo'})


# ── Mixins ──────────────────────────────────────────────────────────────────

class OrdersAccessMixin(LoginRequiredMixin):
    """Drop-in dispatch gate for every internal orders view."""
    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated and not can_use_orders(request.user):
            raise PermissionDenied("You don't have access to the Orders module.")
        return super().dispatch(request, *args, **kwargs)
