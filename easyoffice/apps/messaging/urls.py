from django.urls import path
from apps.messaging import views
from apps.messaging import floating_chat_views

urlpatterns = [
    # ─────────────────────────────────────────────
    # LIST & ROOMS
    # ─────────────────────────────────────────────
    path('',                   views.ChatListView.as_view(),       name='chat_list'),
    path('create/',            views.CreateChatRoomView.as_view(), name='create_chat_room'),
    path('dm/<uuid:user_id>/', views.DirectMessageView.as_view(),  name='direct_message'),
    path('<uuid:room_id>/',    views.ChatRoomView.as_view(),       name='chat_room'),

    # ─────────────────────────────────────────────
    # POLLING (WS FALLBACK)
    # ─────────────────────────────────────────────
    path('<uuid:room_id>/messages/poll/',   views.ChatMessagesPollView.as_view(),  name='chat_messages_poll'),
    path('<uuid:room_id>/reactions-poll/',  views.ChatReactionsPollView.as_view(), name='chat_reactions_poll'),
    path('<uuid:room_id>/edits-poll/',      views.ChatEditsPollView.as_view(),     name='chat_edits_poll'),

    # 🔥 NEW → POLL STATE AUTO REFRESH
    path('<uuid:room_id>/polls/state/',     views.PollStateView.as_view(), name='chat_poll_state'),

    # ─────────────────────────────────────────────
    # POLLS (ADVANCED)
    # ─────────────────────────────────────────────
    path('<uuid:room_id>/poll/create/', views.CreatePollView.as_view(), name='create_chat_poll'),
    path('poll/<uuid:poll_id>/vote/',  views.VotePollView.as_view(),  name='vote_chat_poll'),

    # 🔥 NEW → CLOSE POLL
    path('poll/<uuid:poll_id>/close/', views.ClosePollView.as_view(), name='close_chat_poll'),

    path('notifications/poll/', views.NotificationPollView.as_view(), name='notifications_poll'),

    # ─────────────────────────────────────────────
    # 📌 PIN MESSAGES + 📎 SHARED ITEMS (files+links)
    # ─────────────────────────────────────────────
    path('<uuid:room_id>/message/<uuid:message_id>/pin/',
         views.TogglePinMessageView.as_view(),
         name='toggle_pin_message'),
    path('<uuid:room_id>/pinned/',
         views.PinnedMessagesListView.as_view(),
         name='pinned_messages_list'),
    path('<uuid:room_id>/shared-items/',
         views.ChatRoomSharedItemsView.as_view(),
         name='chat_shared_items'),

    # ─────────────────────────────────────────────
    # 🔎 MESSAGE SEARCH (both in-room and global)
    # ─────────────────────────────────────────────
    path('search/',
         views.MessageSearchView.as_view(),
         name='chat_message_search'),
    # ─────────────────────────────────────────────
    # REACTIONS
    # ─────────────────────────────────────────────
    path(
        '<uuid:room_id>/message/<uuid:message_id>/react/',
        views.ToggleReactionView.as_view(),
        name='toggle_message_reaction',
    ),

    # ─────────────────────────────────────────────
    # MEMBER MANAGEMENT
    # ─────────────────────────────────────────────
    path('<uuid:room_id>/add-members/',                 views.AddRoomMemberView.as_view(),    name='add_room_members'),
    path('<uuid:room_id>/remove-member/<uuid:user_id>/', views.RemoveRoomMemberView.as_view(), name='remove_room_member'),
    path('<uuid:room_id>/delete/',                      views.DeleteRoomView.as_view(),        name='delete_chat_room'),

    # ─────────────────────────────────────────────
    # MESSAGE MANAGEMENT
    # ─────────────────────────────────────────────
    path('api/delete/<uuid:message_id>/', views.DeleteMessageView.as_view(), name='delete_chat_message'),
    path('api/edit/<uuid:message_id>/',   views.EditMessageView.as_view(),   name='edit_chat_message'),

    # ─────────────────────────────────────────────
    # FILES (CHAT + PROJECT)
    # ─────────────────────────────────────────────
    path('<uuid:room_id>/files/',        views.ChatFilePickerAPIView.as_view(),    name='chat_file_picker'),
    path('<uuid:room_id>/attach-file/',  views.ChatAttachSharedFileView.as_view(), name='chat_attach_file'),

    # PIN / UNPIN
    path('<uuid:room_id>/files/pin/',                  views.ChatRoomFilePinView.as_view(),   name='chat_room_file_pin'),
    path('<uuid:room_id>/files/<uuid:file_id>/unpin/', views.ChatRoomFileUnpinView.as_view(), name='chat_room_file_unpin'),

    # 🔥 NEW → GRANT FILE ACCESS FROM CHAT
    path('<uuid:room_id>/files/<uuid:file_id>/grant-access/',
         views.ChatRoomFileGrantAccessView.as_view(),
         name='chat_room_file_grant_access'),

    # ─────────────────────────────────────────────
    # FORWARD & SAVE
    # ─────────────────────────────────────────────
    path('<uuid:room_id>/message/<uuid:message_id>/forward/',
         views.ForwardChatMessageView.as_view(),
         name='forward_chat_message'),

    path('<uuid:room_id>/message/<uuid:message_id>/save-to-files/',
         views.SaveChatMessageToFilesView.as_view(),
         name='save_chat_message_to_files'),

    # ─────────────────────────────────────────────
    # 🔥 @ COMMAND SYSTEM
    # ─────────────────────────────────────────────
    path('<uuid:room_id>/tools/palette/',
         views.ChatToolsPaletteView.as_view(),
         name='chat_tools_palette'),

    path('<uuid:room_id>/tools/action/',
         views.ChatCommandActionView.as_view(),
         name='chat_command_action'),

    path('<uuid:room_id>/spellcheck/',
         views.SpellCheckView.as_view(),
         name='chat_spellcheck'),

    # ─────────────────────────────────────────────
    # PRESENCE
    # ─────────────────────────────────────────────
    # 🩹 FIX: user_id is a UUID across the whole app, not an int.
    # The old <int:user_id> converter never matched, so the frontend's
    # GET /messages/presence/<uuid>/ was silently 404-ing every 12s.
    path('presence/heartbeat/',        views.PresenceHeartbeatView.as_view(), name='presence_heartbeat'),
    path('presence/<uuid:user_id>/',   views.PresenceView.as_view(),          name='presence'),

    # ─────────────────────────────────────────────
    # 🆕 VOICE CALL (1-on-1, WebRTC, NOT recorded)
    # ─────────────────────────────────────────────
    # Cross-page "ring" — lets the callee get notified even if their chat
    # room tab for this DM isn't currently open. Stored in Redis with a
    # short TTL; the callee's browser polls /call/incoming/ every few
    # seconds. All actual audio is WebRTC peer-to-peer; the server only
    # brokers tiny SDP/ICE signalling blobs via the existing WebSocket.
    path('call/ring/<uuid:room_id>/',   views.CallRingView.as_view(),     name='call_ring'),
    path('call/incoming/',              views.CallIncomingView.as_view(), name='call_incoming'),
    path('call/clear/<uuid:room_id>/',  views.CallClearView.as_view(),    name='call_clear'),

    # 🆕 Popup window that OWNS the call (persists across main-window navigation)
    path('call/window/<uuid:room_id>/', views.CallWindowView.as_view(),   name='call_window'),

    # ─────────────────────────────────────────────
    # 🆕 DOCUMENT PRESENTATION (in-call)
    # ─────────────────────────────────────────────
    path('present/<uuid:room_id>/upload/', views.PresentUploadView.as_view(),    name='present_upload'),
    path('present/<uuid:room_id>/files/',  views.PresentListFilesView.as_view(), name='present_files'),

    # ─────────────────────────────────────────────
    # 🆕 GLOBAL FLOATING CHAT WIDGET (corner popup)
    # ─────────────────────────────────────────────
    path(
        'floating/inbox/',
        floating_chat_views.FloatingChatInboxView.as_view(),
        name='floating_chat_inbox',
    ),
    path(
        'floating/<uuid:room_id>/messages/',
        floating_chat_views.FloatingChatRoomMessagesView.as_view(),
        name='floating_chat_messages',
    ),
    path(
        'floating/<uuid:room_id>/send/',
        floating_chat_views.FloatingChatSendView.as_view(),
        name='floating_chat_send',
    ),

]