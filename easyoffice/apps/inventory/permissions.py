"""
apps/inventory/permissions.py
─────────────────────────────
Access control for the inventory module.

Two layers, evaluated in order:

  1. **Dynamic grants** — InventoryAccessGrant rows assigned to a user or
     to one of their departments. This is the primary mechanism. See
     apps.inventory.access for the resolver.

  2. **Legacy group / title fallback** — preserves the historical group
     model so existing deployments don't immediately break. A user who's
     in the 'inventory' group but has no grants yet is treated as having
     OPERATE on the day-to-day modules. As soon as you start granting
     explicitly, the explicit grants take over.

To make the migration painless, run the management command:

    ./manage.py seed_inventory_access

which copies current group memberships into explicit grants.
"""
from django.contrib.auth import get_user_model
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied

from .access import (
    Level, Module, can_manage as can_manage_module,
    can_operate as can_operate_module, can_view as can_view_module,
    effective_access,
)


User = get_user_model()


# ── Legacy group buckets (still used as a fallback) ─────────────────────────

INVENTORY_TEAM_GROUPS = {
    'inventory', 'stock', 'storekeeper', 'store keeper',
    'warehouse', 'logistics',
    'supervisor', 'manager', 'admin', 'administrator', 'ceo',
    'head of operations', 'operations',
}

INVENTORY_MANAGER_GROUPS = {
    'supervisor', 'manager', 'admin', 'administrator', 'ceo',
    'head of operations', 'head of inventory',
}

CEO_GROUPS = {'ceo'}


# ── Helpers ─────────────────────────────────────────────────────────────────

def _user_group_names(user):
    if not user or not getattr(user, 'is_authenticated', False):
        return set()
    return {g.lower() for g in user.groups.values_list('name', flat=True)}


def _has_position_keyword(user, *keywords):
    try:
        title = (user.staffprofile.position.title or '').lower()
    except Exception:
        return False
    return any(kw in title for kw in keywords)


def _has_any_grant(user) -> bool:
    """True if ANY grant touches this user — either via direct grant or
    through a department they belong to. Used to decide whether to fall
    back to the legacy group-based rules."""
    if not user or not getattr(user, 'is_authenticated', False):
        return False
    from .access import _build_cache  # cache hits the same query path
    return bool(_build_cache(user))


# ════════════════════════════════════════════════════════════════════════════
# Public predicates — used by views, templates, and external code
# ════════════════════════════════════════════════════════════════════════════

def can_use_inventory(user):
    """Baseline: log in + see the dashboard. Always True for authenticated
    users (so individuals can at minimum scan / see "my assets")."""
    return bool(user and getattr(user, 'is_authenticated', False))


def can_manage_stock(user):
    """Move stock, edit products, receive POs. Either grant-based OPERATE
    on movements, or legacy team membership."""
    if not user or not getattr(user, 'is_authenticated', False):
        return False
    if user.is_superuser or user.is_staff:
        return True
    # Grant-based: operate on movements OR products
    if (can_operate_module(user, Module.MOVEMENTS)
            or can_operate_module(user, Module.PRODUCTS)):
        return True
    # Legacy fallback — only used when the user has no grants at all
    if _has_any_grant(user):
        return False
    if _user_group_names(user) & INVENTORY_TEAM_GROUPS:
        return True
    return _has_position_keyword(
        user, 'inventory', 'stock', 'storekeeper', 'warehouse',
        'logistics', 'supervisor', 'manager', 'admin', 'ceo',
        'head of', 'operations',
    )


def can_approve_inventory(user):
    """Approve stock requests, finalise stock-takes, edit cost prices."""
    if not user or not getattr(user, 'is_authenticated', False):
        return False
    if user.is_superuser:
        return True
    if (can_manage_module(user, Module.REQUESTS)
            or can_manage_module(user, Module.STOCKTAKE)):
        return True
    if _has_any_grant(user):
        return False
    if _user_group_names(user) & INVENTORY_MANAGER_GROUPS:
        return True
    return _has_position_keyword(
        user, 'supervisor', 'manager', 'admin', 'ceo', 'head of', 'operations',
    )


def can_dispose_assets(user):
    """Retire / dispose / write-off — needs MANAGE on assets, or CEO/admin."""
    if not user or not getattr(user, 'is_authenticated', False):
        return False
    if user.is_superuser:
        return True
    if can_manage_module(user, Module.ASSETS):
        return True
    if _has_any_grant(user):
        return False
    return bool(
        _user_group_names(user) & (CEO_GROUPS | {'admin', 'administrator'})
    )


def can_administer_access(user):
    """Who can grant / revoke access. Lock this down."""
    if not user or not getattr(user, 'is_authenticated', False):
        return False
    if user.is_superuser:
        return True
    if can_manage_module(user, Module.ADMIN):
        return True
    return bool(_user_group_names(user) & {'admin', 'administrator', 'ceo'})


# ── Per-object checks ───────────────────────────────────────────────────────

def can_view_asset(user, asset):
    if can_view_module(user, Module.ASSETS):
        return True
    if can_manage_stock(user):
        return True
    return bool(asset.assigned_to_id and asset.assigned_to_id == user.id)


def can_edit_asset(user, asset):
    return can_manage_stock(user)


def can_view_stock_request(user, sr):
    if can_view_module(user, Module.REQUESTS):
        return True
    if can_manage_stock(user):
        return True
    return sr.requested_by_id == user.id


# ════════════════════════════════════════════════════════════════════════════
# Mixins
# ════════════════════════════════════════════════════════════════════════════

class InventoryAccessMixin(LoginRequiredMixin):
    """
    Drop-in dispatch gate — any authenticated user.

    If the view declares an `inv_module` class attribute, the mixin also
    enforces the dynamic-grant check (defaults to 'view' level — set
    `inv_level = 'operate'` or 'manage' for stricter views). Views without
    `inv_module` keep the legacy behaviour of "any authenticated user".
    """
    inv_module: str = ''
    inv_level: str = 'view'

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return super().dispatch(request, *args, **kwargs)
        if not can_use_inventory(request.user):
            raise PermissionDenied("You don't have access to the Inventory module.")
        # If the view declares a module, enforce the per-module grant.
        if self.inv_module:
            actual = effective_access(request.user, self.inv_module)
            required = Level.to_int(self.inv_level)
            if actual < required:
                # Render a friendly splash, not a flat 403.
                from django.shortcuts import render
                return render(request, 'inventory/no_access.html', {
                    'denied_module': self.inv_module,
                    'denied_level':  self.inv_level,
                }, status=403)
        return super().dispatch(request, *args, **kwargs)


class InventoryManagerMixin(LoginRequiredMixin):
    """Restricts views to stock managers (legacy compat)."""
    inv_module: str = ''
    inv_level: str = 'operate'

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return super().dispatch(request, *args, **kwargs)
        if not can_manage_stock(request.user):
            raise PermissionDenied("You don't have inventory management rights.")
        # Same per-module enforcement when declared
        if self.inv_module:
            actual = effective_access(request.user, self.inv_module)
            required = Level.to_int(self.inv_level)
            if actual < required:
                from django.shortcuts import render
                return render(request, 'inventory/no_access.html', {
                    'denied_module': self.inv_module,
                    'denied_level':  self.inv_level,
                }, status=403)
        return super().dispatch(request, *args, **kwargs)


class ModuleAccessMixin(LoginRequiredMixin):
    """
    Stronger gate: requires a specific (module, level) for the view.

    Usage:
        class MyView(ModuleAccessMixin, ListView):
            inv_module = Module.PRODUCTS
            inv_level  = 'view'        # or 'operate' / 'manage'
    """
    inv_module: str = ''
    inv_level: str = 'view'

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return super().dispatch(request, *args, **kwargs)
        if not self.inv_module:
            return super().dispatch(request, *args, **kwargs)
        actual = effective_access(request.user, self.inv_module)
        required = Level.to_int(self.inv_level)
        if actual < required:
            raise PermissionDenied(
                f"You need '{self.inv_level}' access on the "
                f"{self.inv_module} module."
            )
        return super().dispatch(request, *args, **kwargs)