"""
apps/orders/permissions.py
──────────────────────────
Who can do what with sales orders.

    • View / create order      : Customer Service, Sales, Sales Reps, supervisors,
                                 managers, CEO, superusers
    • Confirm order            : Sales Supervisor / Manager / Admin / CEO only
    • Generate Proforma/       : Order creator OR any orders-team member, BUT
      Invoice / Delivery Note    only when the previous step's signature is
                                 complete (state-machine enforced in services)
    • Sign documents           : CEO only (no fallback per project decision)
    • Cancel                   : Anyone with orders access; cancelled cannot be
                                 un-cancelled
"""
from django.contrib.auth import get_user_model
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied


User = get_user_model()


# ── Group-name buckets (lower-case match) ──────────────────────────────────

ORDERS_TEAM_GROUPS = {
    'customer service', 'customer_service', 'cs',
    'sales', 'sales rep', 'sales_rep',
    'supervisor', 'manager', 'admin', 'administrator', 'ceo',
    'head of sales',
}

# Who can confirm an order (gate before any document generation).
CONFIRM_GROUPS = {
    'supervisor', 'manager', 'admin', 'administrator', 'ceo',
    'head of sales', 'sales supervisor',
}

# CEO group — the ONLY group authorised to sign generated documents.
CEO_GROUPS = {'ceo'}


# ── Helpers ─────────────────────────────────────────────────────────────────

def _user_group_names(user):
    if not user or not getattr(user, 'is_authenticated', False):
        return set()
    return {g.lower() for g in user.groups.values_list('name', flat=True)}


def _has_position_keyword(user, *keywords):
    """Defensive title-based match via StaffProfile (no profile = False)."""
    try:
        title = (user.staffprofile.position.title or '').lower()
    except Exception:
        return False
    return any(kw in title for kw in keywords)


# ── Predicates ──────────────────────────────────────────────────────────────

def can_use_orders(user):
    """View, create, edit own draft orders."""
    if not user or not getattr(user, 'is_authenticated', False):
        return False
    if user.is_superuser or user.is_staff:
        return True
    if _user_group_names(user) & ORDERS_TEAM_GROUPS:
        return True
    return _has_position_keyword(
        user, 'customer service', 'sales', 'supervisor',
        'manager', 'admin', 'ceo', 'head of',
    )


def can_confirm_order(user, order=None):
    """Sales Supervisor / Manager / Admin / CEO only."""
    if not user or not getattr(user, 'is_authenticated', False):
        return False
    if user.is_superuser:
        return True
    if _user_group_names(user) & CONFIRM_GROUPS:
        return True
    return _has_position_keyword(
        user, 'supervisor', 'manager', 'admin', 'ceo', 'head of',
    )


def can_fulfill_order(user, order=None):
    """
    Generate proforma / invoice / DN. Same baseline as orders access — but
    services.py enforces the state machine (e.g. you can't generate an
    invoice until the proforma is signed).
    """
    return can_use_orders(user)


def can_cancel_order(user, order=None):
    return can_use_orders(user)


def can_sign_orders_documents(user):
    """ONLY the CEO. No fallback. Used by the signature handler."""
    if not user or not getattr(user, 'is_authenticated', False):
        return False
    return bool(_user_group_names(user) & CEO_GROUPS)


def find_ceo_user():
    """
    Return the user who should receive document signature requests.

    Strategy:
        1. First active user in the 'CEO' group (case-insensitive)
        2. None — caller must surface a clear error message

    NOTE: per project decision there is no fallback. If the CEO is on leave
    and unsigned documents are piling up, an administrator can temporarily
    add another user to the CEO group, sign, then remove them.
    """
    return (
        User.objects
        .filter(is_active=True, groups__name__iexact='CEO')
        .order_by('pk')
        .first()
    )


# ── Mixins ──────────────────────────────────────────────────────────────────

class OrdersAccessMixin(LoginRequiredMixin):
    """Drop-in dispatch gate for every internal orders view."""
    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated and not can_use_orders(request.user):
            raise PermissionDenied("You don't have access to the Orders module.")
        return super().dispatch(request, *args, **kwargs)