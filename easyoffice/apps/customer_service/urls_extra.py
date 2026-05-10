"""
apps/customer_service/urls_extra.py
───────────────────────────────────
URL routes added on top of your existing urls.py.

This module is included from urls.py at the bottom — keep it that way so
that reverse() works for every name defined here.
"""
from django.urls import path
from . import views_extra

urlpatterns = [
    # ── Ticket-driven flows ──────────────────────────────────────────────
    path(
        'tickets/<int:pk>/create-order/',
        views_extra.CreateOrderFromTicketView.as_view(),
        name='customer_service_create_order_from_ticket',
    ),
    path(
        'tickets/<int:pk>/confirm-close/',
        views_extra.ConfirmAndCloseTicketView.as_view(),
        name='customer_service_confirm_close_ticket',
    ),
    path(
        'assignments/<int:pk>/complete/',
        views_extra.CompleteAssignmentView.as_view(),
        name='customer_service_complete_assignment',
    ),

    # ── Oversight (Head of CS / Head of Sales) ───────────────────────────
    path(
        'oversight/',
        views_extra.HeadDashboardView.as_view(),
        name='customer_service_oversight',
    ),
    path(
        'oversight/report.csv',
        views_extra.HeadReportCSVView.as_view(),
        name='customer_service_oversight_report_csv',
    ),

    # ── Customer-portal bridge ───────────────────────────────────────────
    path(
        'tickets/<int:pk>/portal-reply/',
        views_extra.PortalReplyView.as_view(),
        name='customer_service_portal_reply',
    ),

    # ── Standalone contact creation (no call required) ───────────────────
    # Restricted by views_extra.CustomerCreateView.dispatch — CS team,
    # CS Head, Sales Head, CEO, admins, superusers only.
    path(
        'customers/new/',
        views_extra.CustomerCreateView.as_view(),
        name='customer_service_customer_create',
    ),
    path(
        'customers/<int:pk>/edit/',
        views_extra.CustomerEditView.as_view(),
        name='customer_service_customer_edit',
    ),

    # ── Order directly from a customer (no call / no ticket) ─────────────
    path(
        'customers/<int:pk>/create-order/',
        views_extra.CreateOrderFromCustomerView.as_view(),
        name='customer_service_create_order_from_customer',
    ),
]