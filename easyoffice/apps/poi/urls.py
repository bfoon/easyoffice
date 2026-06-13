"""
apps/poi/urls.py
────────────────
Mount in your root urls.py:

    path("poi/", include("apps.poi.urls")),
"""

from django.urls import path

from apps.poi import auth_views, admin_views, portal_views, tool_views

urlpatterns = [
    # ── Auth / registration ───────────────────────────────────────────────────
    path("login/",  auth_views.POILoginView.as_view(),    name="poi_login"),
    path("login/otp/", auth_views.POILoginOtpView.as_view(), name="poi_login_otp"),
    path("logout/", auth_views.POILogoutView.as_view(),   name="poi_logout"),

    path("register/<uuid:token>/",     auth_views.POIRegisterView.as_view(), name="poi_register"),
    path("register/<uuid:token>/otp/", auth_views.POIOtpView.as_view(),      name="poi_register_otp"),

    # ── Portal shell ──────────────────────────────────────────────────────────
    path("", portal_views.POIPortalView.as_view(), name="poi_portal"),

    # ── Portal data API ───────────────────────────────────────────────────────
    path("api/contacts/",                 portal_views.POIContactsView.as_view(),      name="poi_contacts"),
    path("api/rooms/",                    portal_views.POIRoomsView.as_view(),         name="poi_rooms"),
    path("api/rooms/<uuid:room_id>/messages/", portal_views.POIRoomMessagesView.as_view(), name="poi_room_messages"),
    path("api/rooms/<uuid:room_id>/send/",     portal_views.POIRoomSendView.as_view(),      name="poi_room_send"),
    path("api/rooms/<uuid:room_id>/message/<uuid:message_id>/react/", portal_views.POIRoomReactView.as_view(), name="poi_room_react"),
    path("api/rooms/<uuid:room_id>/message/<uuid:message_id>/edit/",  portal_views.POIRoomEditView.as_view(),  name="poi_room_edit"),
    path("api/rooms/<uuid:room_id>/attach-file/", portal_views.POIRoomAttachFileView.as_view(), name="poi_room_attach_file"),
    path("api/chat/start/",               portal_views.POIStartChatView.as_view(),     name="poi_chat_start"),
    path("api/files/",                    portal_views.POIFilesView.as_view(),         name="poi_files"),
    path("api/files/<uuid:file_id>/preview/", portal_views.POIFilePreviewView.as_view(), name="poi_file_preview"),
    path("api/notifications/",            portal_views.POINotificationsView.as_view(), name="poi_notifications"),

    # ── File tools (scoped) ───────────────────────────────────────────────────
    path("api/files/<uuid:file_id>/to-pdf/",       tool_views.POIToConvertPDFView.as_view(),  name="poi_tool_to_pdf"),
    path("api/files/<uuid:file_id>/to-word/",      tool_views.POIPdfToWordView.as_view(),     name="poi_tool_to_word"),
    path("api/files/<uuid:file_id>/remove-pages/", tool_views.POIRemovePagesView.as_view(),   name="poi_tool_remove_pages"),
    path("api/files/<uuid:file_id>/sign/",         tool_views.POISendForSignatureView.as_view(), name="poi_tool_sign"),
    path("api/tools/merge/",                       tool_views.POIMergePDFView.as_view(),      name="poi_tool_merge"),

    # ── Staff-side management ─────────────────────────────────────────────────
    path("manage/",                  admin_views.POIListView.as_view(),     name="poi_manage_list"),
    path("manage/create/",           admin_views.POICreateView.as_view(),   name="poi_manage_create"),
    path("manage/<uuid:pk>/disable/",  admin_views.POIDisableView.as_view(),  name="poi_manage_disable"),
    path("manage/<uuid:pk>/reinvite/", admin_views.POIReinviteView.as_view(), name="poi_manage_reinvite"),
]