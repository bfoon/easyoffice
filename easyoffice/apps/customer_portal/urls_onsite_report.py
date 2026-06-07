"""
apps/customer_portal/urls_onsite_report.py
===========================================

NEW URL module. Include it from your project urls.py (or merge the
patterns into apps/customer_portal/urls.py):

    # project urls.py
    path('onsite/', include('apps.customer_portal.urls_onsite_report')),

All routes are staff-auth (LoginRequiredMixin) — no public token links.
"""
from django.urls import path

from apps.customer_portal import views_onsite_report as v

urlpatterns = [
    # ── Visit creation ───────────────────────────────────────────────────
    path('contracts/<uuid:contract_pk>/visit/new/',
         v.VisitStartForContractView.as_view(), name='visit_start_contract'),
    path('tickets/<uuid:assignment_pk>/visit/new/',
         v.VisitStartForTicketView.as_view(), name='visit_start_ticket'),

    # ── Visit logging ────────────────────────────────────────────────────
    path('visits/<uuid:pk>/',
         v.VisitReportDetailView.as_view(), name='visit_report_detail'),
    path('visits/<uuid:pk>/activities/add/',
         v.VisitActivityAddView.as_view(), name='visit_activity_add'),
    path('visits/<uuid:pk>/activities/<uuid:act_pk>/edit/',
         v.VisitActivityEditView.as_view(), name='visit_activity_edit'),
    path('visits/<uuid:pk>/activities/<uuid:act_pk>/delete/',
         v.VisitActivityDeleteView.as_view(), name='visit_activity_delete'),
    path('visits/<uuid:pk>/submit/',
         v.VisitSubmitView.as_view(), name='visit_submit'),
    path('visits/<uuid:pk>/reopen/',
         v.VisitReopenView.as_view(), name='visit_reopen'),

    # ── Monthly report ───────────────────────────────────────────────────
    path('monthly/generate/',
         v.MonthlyReportGenerateView.as_view(), name='monthly_report_generate'),
    path('monthly/<uuid:pk>/',
         v.MonthlyReportDetailView.as_view(), name='monthly_report_detail'),
    path('monthly/<uuid:pk>/send-for-signature/',
         v.MonthlyReportSignView.as_view(), name='monthly_report_send_for_signature'),
    path('monthly/<uuid:pk>/send/',
         v.MonthlyReportSendView.as_view(), name='monthly_report_send'),
    path('monthly/<uuid:pk>/download.pdf',
         v.MonthlyReportDownloadView.as_view(), name='monthly_report_download'),
]
