from django.urls import path
from apps.finance import views

urlpatterns = [
    path('', views.FinanceDashboardView.as_view(), name='finance_dashboard'),
    path('budget/', views.BudgetListView.as_view(), name='budget_list'),

    path('purchase/', views.PurchaseRequestView.as_view(), name='purchase_request'),
    path('purchase/<uuid:pk>/', views.PurchaseRequestDetailView.as_view(), name='purchase_detail'),
    path('purchase/<uuid:pk>/action/', views.PurchaseApprovalView.as_view(), name='purchase_approval'),

    path('payments/', views.PaymentListView.as_view(), name='payment_list'),
]