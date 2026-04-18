"""
apps/letterhead/urls.py

Mount in your project's main urls.py:

    path('letterhead/', include('apps.letterhead.urls')),

Resulting URL tree
──────────────────
  GET    /letterhead/                                     Builder SPA
  GET    /letterhead/api/templates/                       List all templates
  POST   /letterhead/api/templates/                       Create new template
  GET    /letterhead/api/templates/<uuid>/                Retrieve one
  PUT    /letterhead/api/templates/<uuid>/                Update (full)
  DELETE /letterhead/api/templates/<uuid>/                Delete
  POST   /letterhead/api/templates/<uuid>/set-active/     Make default/active
  POST   /letterhead/api/templates/<uuid>/logo/           Upload logo image
  GET    /letterhead/api/templates/<uuid>/export/docx/    Server-side Word docx
  GET    /letterhead/api/templates/<uuid>/export/pdf/     Server-side PDF
  GET    /letterhead/api/templates/<uuid>/export/png/     Server-side PNG
"""
from django.urls import path

from .views import (
    LetterheadBuilderView,
    LetterheadListView,
    LetterheadDetailView,
    LetterheadUpdateView,
    LetterheadDeleteView,
    LetterheadSetActiveView,
    LetterheadLogoUploadView,
    LetterheadExportView,
)
from .docx_export import LetterheadDocxExportView

urlpatterns = [
    # ── SPA shell ─────────────────────────────────────────────────────────
    path('', LetterheadBuilderView.as_view(), name='letterhead_builder'),

    # ── Collection ────────────────────────────────────────────────────────
    path(
        'api/templates/',
        LetterheadListView.as_view(),
        name='letterhead_api_list',
    ),

    # ── Single-resource actions ───────────────────────────────────────────
    path(
        'api/templates/<uuid:pk>/',
        LetterheadDetailView.as_view(),
        name='letterhead_api_detail',
    ),
    path(
        'api/templates/<uuid:pk>/update/',
        LetterheadUpdateView.as_view(),
        name='letterhead_api_update',
    ),
    path(
        'api/templates/<uuid:pk>/delete/',
        LetterheadDeleteView.as_view(),
        name='letterhead_api_delete',
    ),
    path(
        'api/templates/<uuid:pk>/set-active/',
        LetterheadSetActiveView.as_view(),
        name='letterhead_api_set_active',
    ),

    # ── Logo upload ───────────────────────────────────────────────────────
    path(
        'api/templates/<uuid:pk>/logo/',
        LetterheadLogoUploadView.as_view(),
        name='letterhead_api_logo',
    ),

    # ── Export ────────────────────────────────────────────────────────────
    # docx must come before the generic <str:fmt> catch-all
    path(
        'api/templates/<uuid:pk>/export/docx/',
        LetterheadDocxExportView.as_view(),
        name='letterhead_api_export_docx',
    ),
    path(
        'api/templates/<uuid:pk>/export/<str:fmt>/',
        LetterheadExportView.as_view(),
        name='letterhead_api_export',
    ),
]