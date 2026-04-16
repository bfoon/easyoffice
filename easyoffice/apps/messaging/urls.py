from django.urls import path
from apps.messaging import views

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

    # 🔥 NEW → POLL STATE AUTO REFRESH
    path('<uuid:room_id>/polls/state/',     views.PollStateView.as_view(), name='chat_poll_state'),

    # ─────────────────────────────────────────────
    # POLLS (ADVANCED)
    # ─────────────────────────────────────────────
    path('<uuid:room_id>/poll/create/', views.CreatePollView.as_view(), name='create_chat_poll'),
    path('poll/<uuid:poll_id>/vote/',  views.VotePollView.as_view(),  name='vote_chat_poll'),

    # 🔥 NEW → CLOSE POLL
    path('poll/<uuid:poll_id>/close/', views.ClosePollView.as_view(), name='close_chat_poll'),

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

    # Fetch suggestions (users, tools, files)
    path('<uuid:room_id>/tools/palette/',
         views.ChatToolsPaletteView.as_view(),
         name='chat_tools_palette'),

    # Execute command (@task, @tracker, etc.)
    path('<uuid:room_id>/tools/action/',
         views.ChatCommandActionView.as_view(),
         name='chat_command_action'),

    path('<uuid:room_id>/spellcheck/',
         views.SpellCheckView.as_view(),
         name='chat_spellcheck'),

    path('presence/heartbeat/', views.PresenceHeartbeatView.as_view(), name='presence_heartbeat'),
    path('presence/<int:user_id>/', views.PresenceView.as_view(), name='presence'),
]