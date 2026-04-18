"""
apps/letterhead/urls.py  —  updated to use pure-Python exports (no Node.js)
"""
from django.urls import path

from .views import (
    LetterheadBuilderView, LetterheadListView, LetterheadDetailView,
    LetterheadUpdateView, LetterheadDeleteView, LetterheadSetActiveView,
    LetterheadLogoUploadView,
    LetterheadTablePresetsView, LetterheadTablePresetDetailView,
    LetterheadTablePresetReorderView, LetterheadTablePresetResetView,
)
from .docx_export import (
    LetterheadDocxExportView,
    LetterheadPdfExportView,
    LetterheadPngExportView,
)

urlpatterns = [
    path('', LetterheadBuilderView.as_view(), name='letterhead_builder'),
    path('api/templates/', LetterheadListView.as_view(), name='letterhead_api_list'),
    path('api/templates/<uuid:pk>/', LetterheadDetailView.as_view(), name='letterhead_api_detail'),
    path('api/templates/<uuid:pk>/update/', LetterheadUpdateView.as_view(), name='letterhead_api_update'),
    path('api/templates/<uuid:pk>/delete/', LetterheadDeleteView.as_view(), name='letterhead_api_delete'),
    path('api/templates/<uuid:pk>/set-active/', LetterheadSetActiveView.as_view(), name='letterhead_api_set_active'),
    path('api/templates/<uuid:pk>/logo/', LetterheadLogoUploadView.as_view(), name='letterhead_api_logo'),
    # Pure-Python exports — no Node.js
    path('api/templates/<uuid:pk>/export/docx/', LetterheadDocxExportView.as_view(), name='letterhead_api_export_docx'),
    path('api/templates/<uuid:pk>/export/pdf/',  LetterheadPdfExportView.as_view(),  name='letterhead_api_export_pdf'),
    path('api/templates/<uuid:pk>/export/png/',  LetterheadPngExportView.as_view(),  name='letterhead_api_export_png'),
    # Table presets
    path('api/templates/<uuid:pk>/tables/', LetterheadTablePresetsView.as_view(), name='letterhead_api_tables'),
    path('api/templates/<uuid:pk>/tables/reorder/', LetterheadTablePresetReorderView.as_view(), name='letterhead_api_tables_reorder'),
    path('api/templates/<uuid:pk>/tables/reset/', LetterheadTablePresetResetView.as_view(), name='letterhead_api_tables_reset'),
    path('api/templates/<uuid:pk>/tables/<str:preset_id>/', LetterheadTablePresetDetailView.as_view(), name='letterhead_api_table_detail'),
]