"""apps/orders/urls.py — internal HTML routes."""
from django.urls import path
from . import views

app_name = 'orders'

urlpatterns = [
    path('',                                 views.OrdersDashboardView.as_view(),     name='dashboard'),
    path('list/',                            views.OrderListView.as_view(),           name='order_list'),
    path('new/',                             views.OrderCreateView.as_view(),         name='order_create'),
    path('<uuid:pk>/',                       views.OrderDetailView.as_view(),         name='order_detail'),
    path('<uuid:pk>/confirm/',               views.OrderConfirmView.as_view(),        name='order_confirm'),
    path('<uuid:pk>/fulfill/',               views.OrderFulfillView.as_view(),        name='order_fulfill'),
    path('<uuid:pk>/cancel/',                views.OrderCancelView.as_view(),         name='order_cancel'),
    path('<uuid:pk>/attach-customer/',       views.OrderAttachCustomerView.as_view(), name='order_attach_customer'),
]
