"""
apps/files/collabora/urls.py

Mount via your project's main urls.py:

    path('files/collabora/', include('apps.files.collabora.urls')),

Resulting URL tree:
  /files/collabora/edit/<uuid>/                           → editor launch page
  /files/collabora/files/<uuid>/presence/                 → JSON list of active editors
  /files/collabora/presence/ping/                         → POST, keepalive
  /files/collabora/presence/end/                          → POST, tab close
  /files/collabora/wopi/files/<uuid>                      → Collabora: CheckFileInfo
  /files/collabora/wopi/files/<uuid>/contents             → Collabora: GET file / POST save
"""
from django.urls import path

from .views import (
    CollaboraEditorView,
    PresencePingView,
    EndSessionView,
    PresenceListView,
)
from .wopi import (
    CheckFileInfoView,
    FileContentsView,
)


urlpatterns = [
    # ── User-facing ────────────────────────────────────────────────────────
    path('edit/<uuid:pk>/',           CollaboraEditorView.as_view(), name='collabora_editor'),
    path('files/<uuid:pk>/presence/', PresenceListView.as_view(),    name='collabora_presence_list'),

    # ── Presence API (browser → Django) ────────────────────────────────────
    path('presence/ping/', PresencePingView.as_view(), name='collabora_presence_ping'),
    path('presence/end/',  EndSessionView.as_view(),   name='collabora_presence_end'),

    # ── WOPI endpoints (Collabora → Django) ────────────────────────────────
    # IMPORTANT: CheckFileInfo has NO trailing slash per WOPI spec.
    path('wopi/files/<uuid:file_id>',          CheckFileInfoView.as_view(), name='wopi_check_file_info'),
    path('wopi/files/<uuid:file_id>/contents', FileContentsView.as_view(),  name='wopi_file_contents'),
]
