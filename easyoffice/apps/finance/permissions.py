"""
apps/finance/permissions.py
============================

Centralized permission gates for the Financial Control Center.

Three roles can access the financial system: Finance, CEO, Admin (plus
superuser). Anyone else is bounced to /my-finance/.

Why a dedicated module
----------------------
The dashboard, analytics, reports, and audit views all share the same access
gate. Keeping the helpers in one place means tightening the rule (e.g. add a
"FinanceViewer" group) is a one-line change.
"""
from django.contrib.auth.mixins import UserPassesTestMixin
from django.contrib import messages
from django.shortcuts import redirect


# ── Group-based role checks ──────────────────────────────────────────────────

FINANCIAL_SYSTEM_GROUPS = ('Finance', 'CEO', 'Admin')


def is_finance(user):
    return user.is_authenticated and (
        user.is_superuser or user.groups.filter(name__in=['Finance', 'Admin']).exists()
    )


def is_ceo(user):
    if not user.is_authenticated:
        return False
    if user.is_superuser:
        return True
    if user.groups.filter(name='CEO').exists():
        return True
    profile = getattr(user, 'staffprofile', None)
    try:
        title = (
            getattr(profile.position, 'title', '')
            if profile and getattr(profile, 'position', None) else ''
        )
    except Exception:
        title = ''
    return str(title).strip().lower() == 'ceo'


def is_admin(user):
    return user.is_authenticated and (
        user.is_superuser or user.groups.filter(name='Admin').exists()
    )


# ── Composite gates ──────────────────────────────────────────────────────────

def can_access_financial_system(user):
    """
    Master gate for the Financial Control Center, all reports, analytics,
    and audit log. Finance / CEO / Admin / superuser only.
    """
    return is_finance(user) or is_ceo(user) or is_admin(user)


def can_export_reports(user):
    """Same as access — anyone who can view can export."""
    return can_access_financial_system(user)


def can_view_audit_log(user):
    """
    Audit log is more sensitive than analytics. Admin and CEO see everything,
    Finance sees their own actions plus org-wide read-only.
    """
    return can_access_financial_system(user)


def can_resolve_anomalies(user):
    """Only CEO + Admin (+ superuser) may mark anomalies as reviewed."""
    return user.is_authenticated and (user.is_superuser or is_ceo(user) or is_admin(user))


# ── Mixin for class-based views ──────────────────────────────────────────────

class FinancialSystemAccessMixin(UserPassesTestMixin):
    """
    Drop on any CBV in the financial system to enforce the master gate.
    On failure: flash + redirect to my-finance (never 403, since most users
    legitimately have a personal finance area).
    """

    def test_func(self):
        return can_access_financial_system(self.request.user)

    def handle_no_permission(self):
        messages.error(
            self.request,
            'The Financial Control Center is restricted to Finance, CEO, and Admin staff.'
        )
        return redirect('my_finance_dashboard')
