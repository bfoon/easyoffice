"""
apps/pos/urls.py
────────────────
Wire into the project urls.py with:

    path('pos/', include('apps.pos.urls')),
"""
from django.urls import path
from . import views

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
]
