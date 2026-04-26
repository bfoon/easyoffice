"""apps/orders/api/urls.py"""
from django.urls import path
from . import views

app_name = 'orders_api'

urlpatterns = [
    path('orders/',                 views.CreateOrderView.as_view(),    name='order_create'),
    path('orders/<uuid:pk>/',       views.OrderDetailAPIView.as_view(), name='order_detail'),
]
