"""
apps/customer_service/urls_extra.py
───────────────────────────────────
New URL routes to add to your existing apps/customer_service/urls.py.

OPTION A — paste these lines into your existing urlpatterns:

    from . import views_extra

    path('tickets/<int:pk>/create-order/',
         views_extra.CreateOrderFromTicketView.as_view(),
         name='customer_service_create_order_from_ticket'),

    path('tickets/<int:pk>/confirm-close/',
         views_extra.ConfirmAndCloseTicketView.as_view(),
         name='customer_service_confirm_close_ticket'),

    path('assignments/<int:pk>/complete/',
         views_extra.CompleteAssignmentView.as_view(),
         name='customer_service_complete_assignment'),

    path('oversight/',
         views_extra.HeadDashboardView.as_view(),
         name='customer_service_oversight'),

    path('oversight/report.csv',
         views_extra.HeadReportCSVView.as_view(),
         name='customer_service_oversight_report_csv'),


OPTION B — include this whole module from your existing urls.py:

    from django.urls import include, path
    urlpatterns += [path('', include('apps.customer_service.urls_extra'))]
"""
from django.urls import path
from . import views_extra

urlpatterns = [
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
]
