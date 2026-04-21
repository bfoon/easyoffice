from django.urls import path
from apps.finance import views

urlpatterns = [
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
    path('payment-requests/',                           views.PaymentRequestListView.as_view(),      name='payment_request_list'),
    path('payment-requests/new/',                       views.PaymentRequestCreateView.as_view(),    name='payment_request_create'),
    path('payment-requests/<uuid:pk>/',                 views.PaymentRequestDetailView.as_view(),    name='payment_request_detail'),
    path('payment-requests/<uuid:pk>/mark-paid/',       views.PaymentRequestMarkPaidView.as_view(),  name='payment_request_mark_paid'),
    path('payment-requests/<uuid:pk>/send/',            views.PaymentRequestSendView.as_view(),      name='payment_request_send'),
    path('payment-requests/<uuid:pk>/cancel/',          views.PaymentRequestCancelView.as_view(),    name='payment_request_cancel'),
    path('invoices/',                                       views.IncomingPaymentRequestListView.as_view(),      name='incoming_payment_request_list'),
    path('invoices/new/',                                   views.IncomingPaymentRequestCreateView.as_view(),    name='incoming_payment_request_create'),
    path('invoices/<uuid:pk>/',                             views.IncomingPaymentRequestDetailView.as_view(),    name='incoming_payment_request_detail'),
    path('invoices/<uuid:pk>/send/',                        views.IncomingPaymentRequestSendView.as_view(),      name='incoming_payment_request_send'),
    path('invoices/<uuid:pk>/reminder/',                    views.IncomingPaymentRequestReminderView.as_view(),  name='incoming_payment_request_reminder'),
    path('invoices/<uuid:pk>/mark-paid/',                   views.IncomingPaymentRequestMarkPaidView.as_view(),  name='incoming_payment_request_mark_paid'),
    path('invoices/<uuid:pk>/add-docs/',                    views.IncomingPaymentRequestAddDocView.as_view(),    name='incoming_payment_request_add_docs'),
    path('invoices/<uuid:pk>/cancel/',                      views.IncomingPaymentRequestCancelView.as_view(),    name='incoming_payment_request_cancel'),
    path('contracts/', views.ContractDashboardView.as_view(), name='contract_dashboard'),
    path('contracts/all/', views.ContractListView.as_view(), name='contract_list'),
    path('contracts/new/', views.ContractCreateView.as_view(), name='contract_create'),
    path('contracts/<uuid:pk>/', views.ContractDetailView.as_view(), name='contract_detail'),
    path('contracts/<uuid:pk>/edit/', views.ContractUpdateView.as_view(), name='contract_update'),

]