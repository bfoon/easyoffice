from django.urls import path
from apps.projects import views

urlpatterns = [
    # ── Core project CRUD ────────────────────────────────────────────────────
    path('', views.ProjectListView.as_view(), name='project_list'),
    path('create/', views.ProjectCreateView.as_view(), name='project_create'),
    path('<uuid:pk>/', views.ProjectDetailView.as_view(), name='project_detail'),
    path('<uuid:pk>/edit/', views.ProjectEditView.as_view(), name='project_edit'),
    path('<uuid:pk>/update/', views.ProjectUpdateStatusView.as_view(), name='project_update'),
    path('<uuid:pk>/milestone/', views.ProjectMilestoneView.as_view(), name='project_milestone'),
    path('<uuid:pk>/milestone/<uuid:mid>/status/', views.MilestoneStatusView.as_view(), name='milestone_status'),
    path('<uuid:pk>/risk/', views.ProjectRiskView.as_view(), name='project_risk'),
    path('<uuid:pk>/gantt-pdf/', views.ProjectGanttPDFView.as_view(), name='project_gantt_pdf'),

    # ── 🗳  Surveys ──────────────────────────────────────────────────────────
    path('<uuid:pk>/surveys/', views.ProjectSurveyListView.as_view(), name='project_surveys'),
    path('<uuid:pk>/surveys/<uuid:sid>/', views.SurveyDetailView.as_view(), name='survey_detail'),
    path('<uuid:pk>/surveys/<uuid:sid>/edit/', views.SurveyEditView.as_view(), name='survey_edit'),
    path('<uuid:pk>/surveys/<uuid:sid>/submit/', views.SurveySubmitView.as_view(), name='survey_submit'),
    path('<uuid:pk>/surveys/<uuid:sid>/toggle/', views.SurveyToggleView.as_view(), name='survey_toggle'),
    path('<uuid:pk>/surveys/<uuid:sid>/delete/', views.SurveyDeleteView.as_view(), name='survey_delete'),
    path('surveys/ip-locate/', views.IPLocateView.as_view(), name='survey_ip_locate'),
    path('surveys/reverse-geocode/', views.ReverseGeocodeView.as_view(), name='survey_reverse_geocode'),
    path(
        "surveys/osm-tiles/<int:z>/<int:x>/<int:y>.png",
        views.OSMTileProxyView.as_view(),
        name="survey_osm_tile_proxy",
    ),
    # Enhanced export — supports ?format=csv|xlsx&save_to_files=0|1
    path(
        '<uuid:pk>/locations/export/',
        views.LocationMapExportCSVView.as_view(),
        name='location_export',
    ),

    # Enhanced survey export — ?format=csv|xlsx&save_to_files=0|1
    path(
        '<uuid:pk>/surveys/<uuid:sid>/export/',
        views.SurveyExportView.as_view(),
        name='survey_export',
    ),

    # Multi-step import: preview + commit
    path(
        '<uuid:pk>/locations/import/preview/',
        views.LocationImportPreviewView.as_view(),
        name='location_import_preview',
    ),
    path(
        '<uuid:pk>/locations/import/commit/',
        views.LocationImportCommitView.as_view(),
        name='location_import_commit',
    ),

    # Pick-from-Files source picker (JSON list)
    path(
        '<uuid:pk>/locations/import/picker/',
        views.ImportFilePickerView.as_view(),
        name='location_import_picker',
    ),

    # Import management — list + unlink/delete/rerun via POST action
    path(
        '<uuid:pk>/locations/imports/',
        views.ProjectImportsView.as_view(),
        name='project_imports',
    ),
    path('<uuid:pk>/milestone/reorder/', views.MilestoneReorderView.as_view(), name='milestone_reorder'),

    # Public survey — no login required (token-based)
    path('surveys/public/<uuid:token>/', views.PublicSurveyView.as_view(), name='survey_public'),

    # Live dashboard API (JSON) + full dashboard page
    path('<uuid:pk>/surveys/<uuid:sid>/dashboard/', views.SurveyDashboardView.as_view(), name='survey_dashboard'),
    path('<uuid:pk>/surveys/<uuid:sid>/dashboard/data/', views.SurveyDashboardDataView.as_view(), name='survey_dashboard_data'),

    # ── 📊  Tracking Sheet ───────────────────────────────────────────────────
    path('<uuid:pk>/tracking/', views.TrackingSheetView.as_view(), name='project_tracking'),
    path('<uuid:pk>/tracking/columns/', views.TrackingSheetUpdateColumnsView.as_view(), name='tracking_columns'),
    path('<uuid:pk>/tracking/row/add/', views.TrackingRowAddView.as_view(), name='tracking_row_add'),
    path('<uuid:pk>/tracking/row/<uuid:rid>/edit/', views.TrackingRowEditView.as_view(), name='tracking_row_edit'),
    path('<uuid:pk>/tracking/row/<uuid:rid>/delete/', views.TrackingRowDeleteView.as_view(), name='tracking_row_delete'),
    path('<uuid:pk>/tracking/export/csv/', views.TrackingSheetExportCSVView.as_view(), name='tracking_export_csv'),

    # ── 📍  Location Map ─────────────────────────────────────────────────────
    path('<uuid:pk>/locations/', views.ProjectLocationMapView.as_view(), name='project_locations'),
    path('<uuid:pk>/locations/add/', views.ProjectLocationAddView.as_view(), name='location_add'),
    path('<uuid:pk>/locations/<uuid:lid>/edit/', views.ProjectLocationEditView.as_view(), name='location_edit'),
    path('<uuid:pk>/locations/<uuid:lid>/delete/', views.ProjectLocationDeleteView.as_view(), name='location_delete'),
    path('<uuid:pk>/locations/import/', views.ProjectLocationImportView.as_view(), name='location_import'),
    path('<uuid:pk>/locations/template/<str:fmt>/', views.LocationTemplateDownloadView.as_view(), name='location_template'),
    path('<uuid:pk>/locations/export/pdf/', views.LocationMapPDFExportView.as_view(), name='location_export_pdf'),
    path('<uuid:pk>/locations/export/html/', views.LocationMapHTMLExportView.as_view(), name='location_export_html'),
    path('<uuid:pk>/locations/tools/plus-code-convert/', views.PlusCodeConvertView.as_view(), name='location_plus_code_convert'),
    path('<pk>/zones/', views.ProjectZoneListView.as_view(), name='project_zones_json'),
    path('<pk>/zones/create/', views.ProjectZoneCreateView.as_view(), name='zone_create'),
    path('<pk>/zones/<zid>/edit/', views.ProjectZoneEditView.as_view(), name='zone_edit'),
    path('<pk>/zones/<zid>/delete/', views.ProjectZoneDeleteView.as_view(), name='zone_delete'),
    path('<pk>/locations/bulk-zone/', views.ProjectLocationBulkZoneView.as_view(), name='location_bulk_zone'),

    # ── 📎  Documents ────────────────────────────────────────────────────────
    path('<uuid:pk>/documents/link/', views.ProjectDocumentLinkView.as_view(), name='project_document_link'),
    path('<uuid:pk>/documents/unlink/<uuid:file_id>/', views.ProjectDocumentUnlinkView.as_view(), name='project_document_unlink'),
]