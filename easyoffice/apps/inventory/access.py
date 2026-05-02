"""
apps/inventory/access.py
────────────────────────
Dynamic access control for the inventory module.

Two axes of access:

    MODULE   — which sub-area of the inventory app (products, assets, …)
    LEVEL    — what the user is allowed to do inside that module
                view    : read-only access, can browse / scan
                operate : create / edit, do day-to-day stock movements
                manage  : approve, finalise, dispose, edit cost prices

Two scopes of grant:

    DEPARTMENT-SCOPED — applies to every active member of a department.
                        Used for "the warehouse team can operate stock
                        movements" type rules.

    USER-SCOPED       — overrides for individual people. A user gets the
                        UNION of every grant that touches them: every
                        active department grant they belong to, plus every
                        user-scoped grant they hold.

`effective_access(user, module)` returns the highest level that applies,
or None if the user has no access to that module. Uses a small per-request
cache to avoid a DB hit on every template tag call.
"""
from __future__ import annotations

from django.utils import timezone


# ── Modules ─────────────────────────────────────────────────────────────────
# Keep these stable — they're stored as strings in the InventoryAccessGrant
# table. Adding a module is fine; renaming one needs a data migration.

class Module:
    DASHBOARD  = 'dashboard'
    PRODUCTS   = 'products'
    ASSETS     = 'assets'
    LOCATIONS  = 'locations'
    SUPPLIERS  = 'suppliers'
    CATEGORIES = 'categories'
    MOVEMENTS  = 'movements'
    REQUESTS   = 'requests'
    STOCKTAKE  = 'stocktake'
    REPORTS    = 'reports'
    SCAN       = 'scan'
    ADMIN      = 'admin'   # access-control settings, audit, etc.

    CHOICES = [
        (DASHBOARD,  'Dashboard'),
        (PRODUCTS,   'Products'),
        (ASSETS,     'Assets'),
        (LOCATIONS,  'Locations'),
        (SUPPLIERS,  'Suppliers'),
        (CATEGORIES, 'Categories'),
        (MOVEMENTS,  'Stock movements'),
        (REQUESTS,   'Stock requests'),
        (STOCKTAKE,  'Cycle counts'),
        (REPORTS,    'Reports'),
        (SCAN,       'QR scanner'),
        (ADMIN,      'Access control'),
    ]
    ALL = [c[0] for c in CHOICES]


# ── Levels (ordered — higher number == more rights) ─────────────────────────

class Level:
    NONE    = 0
    VIEW    = 1
    OPERATE = 2
    MANAGE  = 3

    CHOICES = [
        ('view',    'View only'),
        ('operate', 'Operate (create / edit / move stock)'),
        ('manage',  'Manage (approve, finalise, dispose)'),
    ]

    NAME_TO_INT = {'view': VIEW, 'operate': OPERATE, 'manage': MANAGE}
    INT_TO_NAME = {v: k for k, v in NAME_TO_INT.items()}

    @classmethod
    def to_int(cls, name):
        if name is None:
            return cls.NONE
        if isinstance(name, int):
            return name
        return cls.NAME_TO_INT.get(str(name).lower(), cls.NONE)


# ════════════════════════════════════════════════════════════════════════════
# Resolver
# ════════════════════════════════════════════════════════════════════════════

# Per-request cache attached to the user object. Rebuilt on each request
# because we don't want stale data after a grant is added/removed.
_CACHE_ATTR = '_inv_access_cache'


def _build_cache(user):
    """
    Returns a dict {module: level_int} with the user's effective access
    across all modules, computed once per user instance.
    """
    if not user or not getattr(user, 'is_authenticated', False):
        return {}

    # Superusers and Django staff bypass — they get everything at MANAGE.
    if getattr(user, 'is_superuser', False) or getattr(user, 'is_staff', False):
        return {m: Level.MANAGE for m in Module.ALL}

    # Lazy import to avoid model loading order issues.
    from .models import InventoryAccessGrant

    now = timezone.now()
    grants = (
        InventoryAccessGrant.objects
        .filter(is_active=True)
        .filter(_active_q(now, user))
    )

    out = {}
    for g in grants:
        cur = out.get(g.module, Level.NONE)
        new = Level.to_int(g.level)
        if new > cur:
            out[g.module] = new
    return out


def _active_q(now, user):
    """Q that matches user-scoped grants for this user OR department-scoped
    grants for any department they belong to. Filters out expired grants."""
    from django.db.models import Q
    from .models import InventoryAccessGrant

    department_ids = _user_department_ids(user)

    q = Q(user=user) | Q(department_id__in=department_ids)
    q &= (Q(expires_at__isnull=True) | Q(expires_at__gt=now))
    return q


def _user_department_ids(user):
    """
    Best-effort: pull every Department this user belongs to. Supports the
    StaffProfile pattern this project uses (single department) plus a
    many-to-many `departments` relation if a future migration adds one.
    """
    ids = set()
    # 1. StaffProfile.department FK
    try:
        sp = user.staffprofile
        if sp and getattr(sp, 'department_id', None):
            ids.add(sp.department_id)
        # Some profiles also have a position pointing at a department
        if sp and getattr(sp, 'position', None) and getattr(sp.position, 'department_id', None):
            ids.add(sp.position.department_id)
    except Exception:
        pass

    # 2. Direct M2M, if it exists
    rel = getattr(user, 'departments', None)
    if rel is not None:
        try:
            ids.update(rel.values_list('id', flat=True))
        except Exception:
            pass

    return list(ids)


# ── Public API ──────────────────────────────────────────────────────────────

def effective_access(user, module: str) -> int:
    """
    Returns the highest level (int) the user has on the given module.
    NONE (0) means no access. View / Operate / Manage are 1 / 2 / 3.
    """
    if not user or not getattr(user, 'is_authenticated', False):
        return Level.NONE

    cache = getattr(user, _CACHE_ATTR, None)
    if cache is None:
        cache = _build_cache(user)
        try:
            setattr(user, _CACHE_ATTR, cache)
        except Exception:
            # Some User proxies are immutable in templates — fall back to
            # recomputing every call (slow but safe).
            return cache.get(module, Level.NONE)

    return cache.get(module, Level.NONE)


def has_level(user, module: str, level) -> bool:
    """`level` may be an int or a name ('view', 'operate', 'manage')."""
    return effective_access(user, module) >= Level.to_int(level)


def can_view(user, module: str) -> bool:
    return has_level(user, module, Level.VIEW)


def can_operate(user, module: str) -> bool:
    return has_level(user, module, Level.OPERATE)


def can_manage(user, module: str) -> bool:
    return has_level(user, module, Level.MANAGE)


def visible_modules(user) -> list[str]:
    """List of module keys the user can at least view — for nav rendering."""
    return [m for m in Module.ALL if can_view(user, m)]


def invalidate_cache(user):
    """Call after granting / revoking to refresh the per-request cache."""
    try:
        if hasattr(user, _CACHE_ATTR):
            delattr(user, _CACHE_ATTR)
    except Exception:
        pass
