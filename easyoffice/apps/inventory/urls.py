"""apps/inventory/urls.py — internal HTML routes."""
from django.urls import path

from . import views

app_name = 'inventory'

urlpatterns = [
    # Dashboard
    path('',  views.InventoryDashboardView.as_view(),  name='dashboard'),

    # Products
    path('products/',                 views.ProductListView.as_view(),    name='product_list'),
    path('products/new/',             views.ProductCreateView.as_view(),  name='product_create'),
    path('products/<uuid:pk>/',       views.ProductDetailView.as_view(),  name='product_detail'),
    path('products/<uuid:pk>/edit/',  views.ProductUpdateView.as_view(),  name='product_update'),

    # Stock movements
    path('stock/receive/',  views.ReceiveStockView.as_view(),   name='stock_receive'),
    path('stock/issue/',    views.IssueStockView.as_view(),     name='stock_issue'),
    path('stock/transfer/', views.TransferStockView.as_view(),  name='stock_transfer'),
    path('stock/movements/', views.MovementListView.as_view(),  name='movement_list'),

    # Locations
    path('locations/',                views.LocationListView.as_view(),    name='location_list'),
    path('locations/new/',            views.LocationCreateView.as_view(),  name='location_create'),
    path('locations/<uuid:pk>/',      views.LocationDetailView.as_view(),  name='location_detail'),
    path('locations/<uuid:pk>/edit/', views.LocationUpdateView.as_view(),  name='location_update'),

    # Suppliers
    path('suppliers/',                views.SupplierListView.as_view(),    name='supplier_list'),
    path('suppliers/new/',            views.SupplierCreateView.as_view(),  name='supplier_create'),
    path('suppliers/<uuid:pk>/edit/', views.SupplierUpdateView.as_view(),  name='supplier_update'),

    # Categories
    path('categories/',               views.CategoryListView.as_view(),    name='category_list'),
    path('categories/new/',           views.CategoryCreateView.as_view(),  name='category_create'),

    # Assets
    path('assets/',                       views.AssetListView.as_view(),         name='asset_list'),
    path('assets/new/',                   views.AssetCreateView.as_view(),       name='asset_create'),
    path('assets/<uuid:pk>/',             views.AssetDetailView.as_view(),       name='asset_detail'),
    path('assets/<uuid:pk>/edit/',        views.AssetUpdateView.as_view(),       name='asset_update'),
    path('assets/<uuid:pk>/assign/',      views.AssetAssignView.as_view(),       name='asset_assign'),
    path('assets/<uuid:pk>/return/',      views.AssetReturnView.as_view(),       name='asset_return'),
    path('assets/<uuid:pk>/maintenance/', views.AssetMaintenanceView.as_view(),  name='asset_maintenance'),
    path('assets/<uuid:pk>/scrap/',       views.AssetScrapView.as_view(),        name='asset_scrap'),

    # Stock requests
    path('requests/',                    views.StockRequestListView.as_view(),    name='stock_request_list'),
    path('requests/new/',                views.StockRequestCreateView.as_view(),  name='stock_request_create'),
    path('requests/<uuid:pk>/',          views.StockRequestDetailView.as_view(),  name='stock_request_detail'),
    path('requests/<uuid:pk>/issue/',    views.StockRequestIssueView.as_view(),   name='stock_request_issue'),
    path('requests/<uuid:pk>/reroute/',  views.StockRequestRerouteView.as_view(), name='stock_request_reroute'),

    # Stock takes
    path('stocktake/',              views.StockTakeListView.as_view(),    name='stocktake_list'),
    path('stocktake/new/',          views.StockTakeCreateView.as_view(),  name='stocktake_create'),
    path('stocktake/<uuid:pk>/',    views.StockTakeDetailView.as_view(),  name='stocktake_detail'),

    # Reports
    path('reports/stock/',  views.StockReportView.as_view(),  name='report_stock'),
    path('reports/assets/', views.AssetReportView.as_view(),  name='report_assets'),

    # QR
    path('scan/',                     views.QRScanPageView.as_view(),  name='scan'),
    path('q/<str:token>/',            views.QRResolveView.as_view(),   name='qr_resolve'),
    path('label/<str:kind>/<uuid:pk>/', views.QRLabelView.as_view(),   name='qr_label'),

    # Access control
    path('access/',                   views.AccessControlListView.as_view(),    name='access_list'),
    path('access/<uuid:pk>/revoke/',  views.AccessControlRevokeView.as_view(),  name='access_revoke'),

    # Catalog generator
    path('catalog/',                  views.CatalogBuilderView.as_view(),       name='catalog_builder'),
    path('catalog/render/',           views.CatalogRenderView.as_view(),        name='catalog_render'),
    path('best-sellers/',             views.BestSellersView.as_view(),          name='best_sellers'),
    path('best-sellers/card/',        views.BestSellersCardView.as_view(),      name='best_sellers_card'),
]
