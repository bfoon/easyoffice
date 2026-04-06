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
]