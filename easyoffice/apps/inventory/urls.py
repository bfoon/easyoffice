"""apps/inventory/urls.py — internal HTML routes."""
from django.urls import path

from . import views
from . import license_views

app_name = 'inventory'

urlpatterns = [
    # Dashboard
    path('',  views.InventoryDashboardView.as_view(),  name='dashboard'),

    # Products
    path('products/',                 views.ProductListView.as_view(),    name='product_list'),
    path('products/new/',             views.ProductCreateView.as_view(),  name='product_create'),
    path('products/<uuid:pk>/',       views.ProductDetailView.as_view(),  name='product_detail'),
    path('products/<uuid:pk>/edit/',  views.ProductUpdateView.as_view(),  name='product_update'),
    path('products/import/', views.OdooImportView.as_view(), name='product_import'),

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

    # ── Licences ────────────────────────────────────────────────────────────
    path('licenses/',                      license_views.LicenseDashboardView.as_view(),   name='license_dashboard'),
    path('licenses/register/',             license_views.LicenseListView.as_view(),        name='license_list'),
    path('licenses/new/',                  license_views.LicenseCreateView.as_view(),      name='license_create'),
    path('licenses/mine/',                 license_views.MyLicensesView.as_view(),         name='my_licenses'),
    path('licenses/report/',               license_views.LicenseReportView.as_view(),      name='license_report'),
    path('licenses/run-expiry-check/',     license_views.LicenseExpiryRunView.as_view(),   name='license_run_check'),

    path('licenses/types/',                license_views.LicenseTypeListView.as_view(),    name='license_type_list'),
    path('licenses/types/new/',            license_views.LicenseTypeCreateView.as_view(),  name='license_type_create'),
    path('licenses/types/<uuid:pk>/edit/', license_views.LicenseTypeUpdateView.as_view(),  name='license_type_update'),

    path('licenses/<uuid:pk>/',            license_views.LicenseDetailView.as_view(),      name='license_detail'),
    path('licenses/<uuid:pk>/edit/',       license_views.LicenseUpdateView.as_view(),      name='license_update'),
    path('licenses/<uuid:pk>/seats/add/',  license_views.LicenseSeatAssignView.as_view(),  name='license_seat_assign'),
    path('licenses/<uuid:pk>/renew/',      license_views.LicenseRenewView.as_view(),       name='license_renew'),
    path('licenses/<uuid:pk>/status/',     license_views.LicenseStatusView.as_view(),      name='license_status'),
    path('licenses/seats/<uuid:pk>/release/',
         license_views.LicenseSeatReleaseView.as_view(),                                  name='license_seat_release'),

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
