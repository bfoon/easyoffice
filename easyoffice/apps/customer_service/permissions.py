"""
apps/customer_service/permissions.py
────────────────────────────────────
Role predicates for the customer-service module.

Heads ('Head of CS' / 'Head of Sales') are matched on EITHER:
    • Django group name (case-insensitive, several common variants)
    • StaffProfile.position.title containing a keyword

This dual-match is intentional: in practice most projects use one mechanism
but new admins often forget which, so we accept both.
"""
from django.contrib.auth import get_user_model
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied

User = get_user_model()


# ── Group buckets (lower-case match) ───────────────────────────────────────

CS_TEAM_GROUPS = {
    'customer service', 'customer_service', 'cs',
    'sales and marketing', 'marketing',
}

CS_MANAGER_GROUPS = {
    'head of customer service', 'head of cs', 'cs head', 'cs manager',
    'customer service manager', 'customer service supervisor',
    'supervisor', 'manager', 'admin', 'administrator',
}

SALES_HEAD_GROUPS = {
    'head of sales', 'sales head', 'sales manager', 'sales supervisor',
}


# ── Helpers ────────────────────────────────────────────────────────────────

def _user_group_names(user) -> set:
    if not user or not getattr(user, 'is_authenticated', False):
        return set()
    return {g.lower() for g in user.groups.values_list('name', flat=True)}


def _has_position_keyword(user, *keywords) -> bool:
    """Defensive title-based match via StaffProfile (no profile = False)."""
    try:
        title = (user.staffprofile.position.title or '').lower()
    except Exception:
        return False
    return any(kw in title for kw in keywords)


# ── Predicates ─────────────────────────────────────────────────────────────

def can_use_customer_service(user) -> bool:
    """View dashboard, take calls, work on tickets."""
    if not user or not getattr(user, 'is_authenticated', False):
        return False
    if user.is_superuser or user.is_staff:
        return True
    if _user_group_names(user) & (CS_TEAM_GROUPS | CS_MANAGER_GROUPS):
        return True
    return _has_position_keyword(
        user, 'customer service', 'cs', 'sales', 'support',
        'supervisor', 'manager', 'head of', 'admin',
    )


def can_manage_department(user) -> bool:
    """
    Assign / route / escalate. Anyone in the CS team can do this for their
    own department; supervisors/heads can do it across departments.
    """
    if not user or not getattr(user, 'is_authenticated', False):
        return False
    if user.is_superuser:
        return True
    if _user_group_names(user) & (CS_MANAGER_GROUPS | CS_TEAM_GROUPS):
        return True
    return _has_position_keyword(
        user, 'supervisor', 'manager', 'head of', 'admin',
        'customer service', 'sales',
    )


def is_head_of_customer_service(user) -> bool:
    if not user or not getattr(user, 'is_authenticated', False):
        return False
    if user.is_superuser:
        return True
    groups = _user_group_names(user)
    if groups & {'head of customer service', 'head of cs', 'cs head'}:
        return True
    return _has_position_keyword(
        user, 'head of customer service', 'head of cs', 'customer service head',
    )


def is_head_of_sales(user) -> bool:
    if not user or not getattr(user, 'is_authenticated', False):
        return False
    if user.is_superuser:
        return True
    if _user_group_names(user) & SALES_HEAD_GROUPS:
        return True
    return _has_position_keyword(user, 'head of sales', 'sales head', 'sales manager')


def can_view_full_oversight(user) -> bool:
    """Either head can see the oversight dashboard / reports."""
    return is_head_of_customer_service(user) or is_head_of_sales(user) or (
        user and getattr(user, 'is_superuser', False)
    )


def can_close_ticket(user, ticket) -> bool:
    """
    Closing the ticket is the *owner's* call. Allow:
        • the ticket creator
        • the current owner
        • any CS supervisor / head / superuser
    """
    if not user or not getattr(user, 'is_authenticated', False):
        return False
    if user.is_superuser:
        return True
    if ticket and (ticket.created_by_id == user.pk or ticket.current_owner_id == user.pk):
        return True
    return is_head_of_customer_service(user) or _user_group_names(user) & CS_MANAGER_GROUPS


# ── Mixins ─────────────────────────────────────────────────────────────────

class CustomerServiceAccessMixin(LoginRequiredMixin):
    """Drop-in dispatch gate for any CS view."""
    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated and not can_use_customer_service(request.user):
            raise PermissionDenied("You don't have access to the Customer Service module.")
        return super().dispatch(request, *args, **kwargs)


class CustomerServiceOversightMixin(LoginRequiredMixin):
    """Restricts views to Heads of CS / Sales."""
    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated and not can_view_full_oversight(request.user):
            raise PermissionDenied(
                'This view is reserved for the Head of Customer Service '
                'and Head of Sales.'
            )
        return super().dispatch(request, *args, **kwargs)


# ── Customer (contact) management ──────────────────────────────────────────
#
# Who may create a customer/contact record OUTSIDE the call-desk flow
# (i.e. without first logging an inbound call)?
#
# Per project decision: CS team, CS Head, Sales Head, CEO, admins,
# superusers. Front-line agents stay free to add customers from the
# call desk as before — this predicate exists so that the standalone
# "+ New Contact" button only appears for people who should see it.

CEO_GROUPS = {'ceo'}


def can_create_customer(user) -> bool:
    """
    Standalone customer/contact creation (no call required).

    Allowed:
      • Superuser / Django staff
      • Anyone in CS team or CS manager groups
      • Head of Sales
      • CEO group
      • StaffProfile titles containing: customer service / sales /
        supervisor / manager / head of / admin / ceo
    """
    if not user or not getattr(user, 'is_authenticated', False):
        return False
    if user.is_superuser or user.is_staff:
        return True
    if _user_group_names(user) & (
        CS_TEAM_GROUPS | CS_MANAGER_GROUPS | SALES_HEAD_GROUPS | CEO_GROUPS
    ):
        return True
    return _has_position_keyword(
        user, 'customer service', 'cs', 'sales',
        'supervisor', 'manager', 'head of', 'admin', 'ceo',
    )


class CustomerManagementMixin(LoginRequiredMixin):
    """Drop-in dispatch gate for views that create or edit Customer records."""
    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated and not can_create_customer(request.user):
            raise PermissionDenied(
                "You don't have permission to create or edit customer contacts."
            )
        return super().dispatch(request, *args, **kwargs)