"""apps/orders/urls.py — internal HTML routes."""
from django.urls import path
from . import views

app_name = 'orders'

urlpatterns = [
    path('',                                  views.OrdersDashboardView.as_view(),               name='dashboard'),
    path('list/',                             views.OrderListView.as_view(),                     name='order_list'),
    path('new/',                              views.OrderCreateView.as_view(),                   name='order_create'),
    path('<uuid:pk>/',                        views.OrderDetailView.as_view(),                   name='order_detail'),
    path('<uuid:pk>/confirm/',                views.OrderConfirmView.as_view(),                  name='order_confirm'),
    path('<uuid:pk>/generate-proforma/',      views.OrderGenerateProformaView.as_view(),         name='order_generate_proforma'),
    # Backwards-compat alias — old templates / API integrations call /fulfill/
    path('<uuid:pk>/fulfill/',                views.OrderGenerateProformaView.as_view(),         name='order_fulfill'),
    path('<uuid:pk>/generate-invoice/',       views.OrderGenerateInvoiceView.as_view(),          name='order_generate_invoice'),
    path('<uuid:pk>/generate-delivery-note/', views.OrderGenerateDeliveryNoteView.as_view(),     name='order_generate_delivery_note'),
    path('<uuid:pk>/cancel/',                 views.OrderCancelView.as_view(),                   name='order_cancel'),
    path('<uuid:pk>/attach-customer/',        views.OrderAttachCustomerView.as_view(),           name='order_attach_customer'),
    # CEO one-click sign
    path('<uuid:pk>/sign-with-saved/',        views.OrderQuickCEOSignView.as_view(),             name='order_sign_with_saved'),
]