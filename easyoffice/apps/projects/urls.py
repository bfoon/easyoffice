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

    # ── 🗳  Quick Survey ─────────────────────────────────────────────────────
    path('<uuid:pk>/surveys/', views.ProjectSurveyListView.as_view(), name='project_surveys'),
    path('<uuid:pk>/surveys/<uuid:sid>/', views.SurveyDetailView.as_view(), name='survey_detail'),
    path('<uuid:pk>/surveys/<uuid:sid>/submit/', views.SurveySubmitView.as_view(), name='survey_submit'),
    path('<uuid:pk>/surveys/<uuid:sid>/toggle/', views.SurveyToggleView.as_view(), name='survey_toggle'),
    path('<uuid:pk>/surveys/<uuid:sid>/delete/', views.SurveyDeleteView.as_view(), name='survey_delete'),

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
    path('<uuid:pk>/locations/export/html/', views.LocationMapHTMLExportView.as_view(), name='location_export_html'),
    path('<uuid:pk>/locations/export/pdf/', views.LocationMapPDFExportView.as_view(), name='location_export_pdf'),
]