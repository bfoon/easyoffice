from django.urls import path
from apps.finance import views
from apps.finance import views_financial_system as fs
from apps.finance import views_invoice_followup

urlpatterns = [
    # ── Existing finance URLs ────────────────────────────────────────────────
    path('', views.FinanceDashboardView.as_view(), name='finance_dashboard'),
    path('budget/', views.BudgetListView.as_view(), name='budget_list'),

    path('purchase/', views.PurchaseRequestView.as_view(), name='purchase_request'),
    path('purchase/<uuid:pk>/', views.PurchaseRequestDetailView.as_view(), name='purchase_detail'),
    path('purchase/<uuid:pk>/action/', views.PurchaseApprovalView.as_view(), name='purchase_approval'),

    path('payments/', views.PaymentListView.as_view(), name='payment_list'),

    path('my-finance/', views.MyFinanceDashboardView.as_view(), name='my_finance_dashboard'),
    path('my-finance/requests/', views.EmployeeFinanceRequestView.as_view(), name='employee_finance_request'),
    path('my-finance/requests/<uuid:pk>/action/', views.EmployeeFinanceRequestActionView.as_view(), name='employee_finance_request_action'),
    path('my-finance/loans/', views.MyLoanListView.as_view(), name='my_loan_list'),

    path('finance-requests/', views.FinanceRequestQueueView.as_view(), name='finance_request_queue'),
    path('finance-requests/<uuid:pk>/process/', views.FinanceRequestProcessView.as_view(), name='finance_request_process'),

    path('payment-requests/', views.PaymentRequestListView.as_view(), name='payment_request_list'),
    path('payment-requests/new/', views.PaymentRequestCreateView.as_view(), name='payment_request_create'),
    path('payment-requests/<uuid:pk>/', views.PaymentRequestDetailView.as_view(), name='payment_request_detail'),
    path('payment-requests/<uuid:pk>/mark-paid/', views.PaymentRequestMarkPaidView.as_view(), name='payment_request_mark_paid'),
    path('payment-requests/<uuid:pk>/send/', views.PaymentRequestSendView.as_view(), name='payment_request_send'),
    path('payment-requests/<uuid:pk>/cancel/', views.PaymentRequestCancelView.as_view(), name='payment_request_cancel'),

    path('invoices/', views.IncomingPaymentRequestListView.as_view(), name='incoming_payment_request_list'),
    path('invoices/new/', views.IncomingPaymentRequestCreateView.as_view(), name='incoming_payment_request_create'),
    path('invoices/<uuid:pk>/', views.IncomingPaymentRequestDetailView.as_view(), name='incoming_payment_request_detail'),
    path('invoices/<uuid:pk>/send/', views.IncomingPaymentRequestSendView.as_view(), name='incoming_payment_request_send'),
    path('invoices/<uuid:pk>/reminder/', views.IncomingPaymentRequestReminderView.as_view(), name='incoming_payment_request_reminder'),
    path('invoices/<uuid:pk>/mark-paid/', views.IncomingPaymentRequestMarkPaidView.as_view(), name='incoming_payment_request_mark_paid'),
    path('invoices/<uuid:pk>/add-docs/', views.IncomingPaymentRequestAddDocView.as_view(), name='incoming_payment_request_add_docs'),
    path('invoices/<uuid:pk>/cancel/', views.IncomingPaymentRequestCancelView.as_view(), name='incoming_payment_request_cancel'),
    path(
        'invoices/<uuid:pk>/follow-up/',
        views_invoice_followup.IncomingPaymentRequestAddFollowUpView.as_view(),
        name='incoming_payment_request_add_followup'
    ),

    path('contracts/', views.ContractDashboardView.as_view(), name='contract_dashboard'),
    path('contracts/all/', views.ContractListView.as_view(), name='contract_list'),
    path('contracts/new/', views.ContractCreateView.as_view(), name='contract_create'),
    path('contracts/<uuid:pk>/', views.ContractDetailView.as_view(), name='contract_detail'),
    path('contracts/<uuid:pk>/edit/', views.ContractUpdateView.as_view(), name='contract_update'),
    path('contracts/<uuid:pk>/generate-invoice/', views.ContractGenerateInvoiceView.as_view(), name='contract_generate_invoice'),
    path('contracts/<uuid:pk>/extend/', views.ContractExtendView.as_view(), name='contract_extend'),
    path('contracts/<uuid:pk>/extensions/<uuid:ext_pk>/delete/',
         views.ContractExtensionDeleteView.as_view(),
         name='contract_extension_delete'),

    # Document generation & e-signature (logged-in)
    path('contracts/<uuid:pk>/generate-document/',
         views.ContractGenerateDocumentView.as_view(),
         name='contract_generate_document'),
    path('contracts/<uuid:pk>/documents/<uuid:doc_pk>/download/',
         views.ContractDocumentDownloadView.as_view(),
         name='contract_document_download'),
    path('contracts/<uuid:pk>/send-for-signature/',
         views.ContractSendForSignatureView.as_view(),
         name='contract_send_for_signature'),
    path('contracts/<uuid:pk>/signature-requests/<uuid:req_pk>/resend/',
         views.ContractSignatureResendView.as_view(),
         name='contract_signature_resend'),
    path('contracts/<uuid:pk>/signature-requests/<uuid:req_pk>/void/',
         views.ContractSignatureVoidView.as_view(),
         name='contract_signature_void'),
    path('contracts/<uuid:pk>/signature-requests/<uuid:req_pk>/signed.pdf',
         views.ContractSignedDownloadView.as_view(),
         name='contract_signature_signed_pdf'),

    # Public counterparty-facing signing page (no login)
    path('contracts/sign/<str:token>/',
         views.ContractPublicSignView.as_view(),
         name='contract_public_sign'),
    path('contracts/sign/<str:token>/document.pdf',
         views.ContractPublicSourceDownloadView.as_view(),
         name='contract_public_source_pdf'),
    path('contracts/sign/<str:token>/signed.pdf',
         views.ContractPublicSignedDownloadView.as_view(),
         name='contract_public_signed_pdf'),

    # ════════════════════════════════════════════════════════════════════════
    # FINANCIAL CONTROL CENTER — analytics, reports, audit, anomalies, invoices
    # ════════════════════════════════════════════════════════════════════════

    # The new dashboard (CFO view)
    path('control-center/', fs.FinancialControlCenterView.as_view(), name='financial_control_center'),
    path('control-center/data/', fs.DashboardDataAPI.as_view(), name='financial_dashboard_data'),

    # On-screen reports
    path('reports/income-statement/', fs.IncomeStatementView.as_view(), name='report_income_statement'),
    path('reports/cash-flow/', fs.CashFlowView.as_view(), name='report_cash_flow'),
    path('reports/budget-variance/', fs.BudgetVarianceView.as_view(), name='report_budget_variance'),
    path('reports/ageing/', fs.AgeingReportView.as_view(), name='report_ageing'),

    # Exports
    path('reports/income-statement/export.pdf', fs.IncomeStatementExportPDF.as_view(), name='export_income_statement_pdf'),
    path('reports/income-statement/export.xlsx', fs.IncomeStatementExportXLSX.as_view(), name='export_income_statement_xlsx'),
    path('reports/cash-flow/export.pdf', fs.CashFlowExportPDF.as_view(), name='export_cash_flow_pdf'),
    path('reports/cash-flow/export.xlsx', fs.CashFlowExportXLSX.as_view(), name='export_cash_flow_xlsx'),
    path('reports/budget-variance/export.pdf', fs.BudgetVarianceExportPDF.as_view(), name='export_budget_variance_pdf'),
    path('reports/budget-variance/export.xlsx', fs.BudgetVarianceExportXLSX.as_view(), name='export_budget_variance_xlsx'),
    path('reports/audit-trail/export.pdf', fs.AuditTrailExportPDF.as_view(), name='export_audit_trail_pdf'),
    path('reports/audit-trail/export.xlsx', fs.AuditTrailExportXLSX.as_view(), name='export_audit_trail_xlsx'),
    path('reports/ageing/export.pdf', fs.AgeingExportPDF.as_view(), name='export_ageing_pdf'),
    path('reports/ageing/export.xlsx', fs.AgeingExportXLSX.as_view(), name='export_ageing_xlsx'),

    # Audit
    path('audit/', fs.AuditLogView.as_view(), name='finance_audit_log'),
    path('audit/<uuid:pk>/', fs.AuditEntryDetailView.as_view(), name='finance_audit_entry'),

    # Anomalies
    path('anomalies/', fs.AnomalyListView.as_view(), name='finance_anomaly_list'),
    path('anomalies/<uuid:pk>/resolve/', fs.AnomalyResolveView.as_view(), name='finance_anomaly_resolve'),

    # Invoices (finance-side list + mark-paid action)
    path('control-center/invoices/',
         fs.FinanceInvoiceListView.as_view(),
         name='finance_invoice_list'),
    path('control-center/invoices/<uuid:pk>/mark-paid/',
         fs.FinanceInvoiceMarkPaidView.as_view(),
         name='finance_invoice_mark_paid'),
]