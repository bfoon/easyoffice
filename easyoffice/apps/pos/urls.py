"""
apps/pos/urls.py
────────────────
Wire into the project urls.py with:

    path('pos/', include('apps.pos.urls')),
"""
from django.urls import path
from . import views
from . import views_cash

urlpatterns = [
    path('',                        views.POSTerminalView.as_view(),   name='pos_terminal'),

    # Product lookup
    path('api/lookup/',             views.ProductLookupView.as_view(), name='pos_lookup'),
    path('api/search/',             views.ProductSearchView.as_view(), name='pos_search'),

    # Basket
    path('api/basket/add/',         views.BasketAddView.as_view(),      name='pos_basket_add'),
    path('api/basket/qty/',         views.BasketQtyView.as_view(),      name='pos_basket_qty'),
    path('api/basket/discount/',    views.BasketDiscountView.as_view(), name='pos_basket_discount'),
    path('api/basket/clear/',       views.BasketClearView.as_view(),    name='pos_basket_clear'),

    # Completion
    path('api/complete/',           views.CompleteSaleView.as_view(),   name='pos_complete'),

    # Receipts (staff)
    path('receipt/<uuid:pk>/',      views.ReceiptView.as_view(),        name='pos_receipt'),
    path('receipt/<uuid:pk>/pdf/',  views.ReceiptPDFView.as_view(),     name='pos_receipt_pdf'),
    path('receipt/<uuid:pk>/email/', views.ReceiptEmailView.as_view(),  name='pos_receipt_email'),

    # Public digital receipt (QR target — no login)
    path('r/<uuid:token>/',         views.ReceiptPublicView.as_view(),  name='pos_receipt_public'),

    # Day book
    path('sales/',                  views.SalesTodayView.as_view(),     name='pos_sales_today'),
    path('sales/<uuid:pk>/void/',   views.VoidSaleView.as_view(),       name='pos_void_sale'),

    # ── Cash drawer sessions (opening / closing balance) ─────────────────
    path('cash/',                   views_cash.CashSessionListView.as_view(),   name='pos_cash_session_list'),
    path('cash/status/',            views_cash.CashSessionStatusView.as_view(), name='pos_cash_status'),
    path('cash/open/',              views_cash.CashSessionOpenView.as_view(),   name='pos_cash_open'),
    path('cash/close/',             views_cash.CashSessionCloseView.as_view(),  name='pos_cash_close'),
    path('cash/movement/',          views_cash.CashMovementView.as_view(),      name='pos_cash_movement'),
    path('cash/<uuid:pk>/',         views_cash.CashSessionDetailView.as_view(), name='pos_cash_session_detail'),
    path('cash/<uuid:pk>/close/',   views_cash.CashSessionSupervisorCloseView.as_view(), name='pos_cash_supervisor_close'),
]
