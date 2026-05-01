"""apps/inventory/api/urls.py — DRF routes for the inventory public API."""
from django.urls import include, path
from rest_framework.routers import DefaultRouter

from . import views


router = DefaultRouter()
router.register(r'products',  views.ProductViewSet,        basename='inventory-product')
router.register(r'locations', views.LocationViewSet,       basename='inventory-location')
router.register(r'suppliers', views.SupplierViewSet,       basename='inventory-supplier')
router.register(r'assets',    views.AssetViewSet,          basename='inventory-asset')
router.register(r'movements', views.StockMovementViewSet,  basename='inventory-movement')


urlpatterns = [
    path('', include(router.urls)),
    path('qr/resolve/', views.QRResolveAPIView.as_view(), name='inventory-qr-resolve'),
]
