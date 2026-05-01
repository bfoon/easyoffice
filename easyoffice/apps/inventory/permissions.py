"""
apps/inventory/permissions.py
─────────────────────────────
Who can do what in the inventory module.

Roles
─────
    • Storekeeper / Inventory clerk : day-to-day stock movements,
                                      assigning assets, creating products
    • Supervisor / Manager / Admin  : approve stock requests, do stock-takes,
                                      retire assets, edit cost prices
    • CEO                           : everything (signs off on disposal /
                                      high-value purchases via finance app)
    • Regular employee              : raise a StockRequest, view "what's
                                      assigned to me", scan QR codes

Patterns mirror apps/orders/permissions.py — Django groups + StaffProfile
title fallback, with predicate functions and a dispatch mixin.
"""
from django.contrib.auth import get_user_model
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied


User = get_user_model()


# ── Group buckets (lower-case match) ────────────────────────────────────────

INVENTORY_TEAM_GROUPS = {
    'inventory', 'stock', 'storekeeper', 'store keeper',
    'warehouse', 'logistics',
    'supervisor', 'manager', 'admin', 'administrator', 'ceo',
    'head of operations', 'operations',
}

# Higher-tier ops — approve, finalize stock-takes, retire assets
INVENTORY_MANAGER_GROUPS = {
    'supervisor', 'manager', 'admin', 'administrator', 'ceo',
    'head of operations', 'head of inventory',
}

# CEO-only actions (asset disposal, write-off above threshold)
CEO_GROUPS = {'ceo'}


# ── Helpers ─────────────────────────────────────────────────────────────────

def _user_group_names(user):
    if not user or not getattr(user, 'is_authenticated', False):
        return set()
    return {g.lower() for g in user.groups.values_list('name', flat=True)}


def _has_position_keyword(user, *keywords):
    """Title-based fallback via StaffProfile (no profile = False)."""
    try:
        title = (user.staffprofile.position.title or '').lower()
    except Exception:
        return False
    return any(kw in title for kw in keywords)


# ── Predicates ──────────────────────────────────────────────────────────────

def can_use_inventory(user):
    """Baseline access: view dashboard, scan, list products/assets."""
    if not user or not getattr(user, 'is_authenticated', False):
        return False
    # All authenticated users get basic visibility (so they can see what's
    # assigned to them, raise a stock request, scan a QR code on a desk).
    return True


def can_manage_stock(user):
    """Move stock, edit products, receive POs, assign assets."""
    if not user or not getattr(user, 'is_authenticated', False):
        return False
    if user.is_superuser or user.is_staff:
        return True
    if _user_group_names(user) & INVENTORY_TEAM_GROUPS:
        return True
    return _has_position_keyword(
        user, 'inventory', 'stock', 'storekeeper', 'warehouse',
        'logistics', 'supervisor', 'manager', 'admin', 'ceo',
        'head of', 'operations',
    )


def can_approve_inventory(user):
    """Approve stock requests, finalize stock-takes, edit cost prices."""
    if not user or not getattr(user, 'is_authenticated', False):
        return False
    if user.is_superuser:
        return True
    if _user_group_names(user) & INVENTORY_MANAGER_GROUPS:
        return True
    return _has_position_keyword(
        user, 'supervisor', 'manager', 'admin', 'ceo',
        'head of', 'operations',
    )


def can_dispose_assets(user):
    """Retire / dispose / write-off — CEO + admin only."""
    if not user or not getattr(user, 'is_authenticated', False):
        return False
    if user.is_superuser:
        return True
    return bool(
        _user_group_names(user) & (CEO_GROUPS | {'admin', 'administrator'})
    )


# ── Per-object checks (mirroring _can_view_purchase shape) ──────────────────

def can_view_asset(user, asset):
    if can_manage_stock(user):
        return True
    # An employee can always see assets currently assigned to them.
    return bool(asset.assigned_to_id and asset.assigned_to_id == user.id)


def can_edit_asset(user, asset):
    return can_manage_stock(user)


def can_view_stock_request(user, sr):
    if can_manage_stock(user):
        return True
    return sr.requested_by_id == user.id


# ── Mixins ──────────────────────────────────────────────────────────────────

class InventoryAccessMixin(LoginRequiredMixin):
    """Drop-in dispatch gate for the inventory module."""
    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated and not can_use_inventory(request.user):
            raise PermissionDenied("You don't have access to the Inventory module.")
        return super().dispatch(request, *args, **kwargs)


class InventoryManagerMixin(LoginRequiredMixin):
    """Restricts views to stock managers / supervisors."""
    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated and not can_manage_stock(request.user):
            raise PermissionDenied("You don't have inventory management rights.")
        return super().dispatch(request, *args, **kwargs)
