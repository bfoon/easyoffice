from django.urls import path
from apps.messaging import views

urlpatterns = [
    # ── List & room ──────────────────────────────────────────────────────────
    path('',                   views.ChatListView.as_view(),    name='chat_list'),
    path('create/',            views.CreateChatRoomView.as_view(), name='create_chat_room'),
    path('dm/<uuid:user_id>/', views.DirectMessageView.as_view(), name='direct_message'),

    path('<uuid:room_id>/',    views.ChatRoomView.as_view(),    name='chat_room'),

    # ── Polling (WS fallback) ────────────────────────────────────────────────
    path('<uuid:room_id>/messages/poll/',    views.ChatMessagesPollView.as_view(),  name='chat_messages_poll'),
    path('<uuid:room_id>/reactions-poll/',   views.ChatReactionsPollView.as_view(), name='chat_reactions_poll'),

    # ── Reactions ────────────────────────────────────────────────────────────
    path(
        '<uuid:room_id>/message/<uuid:message_id>/react/',
        views.ToggleReactionView.as_view(),
        name='toggle_message_reaction',
    ),

    # ── Member management ────────────────────────────────────────────────────
    path('<uuid:room_id>/add-members/',               views.AddRoomMemberView.as_view(),    name='add_room_members'),
    path('<uuid:room_id>/remove-member/<uuid:user_id>/', views.RemoveRoomMemberView.as_view(), name='remove_room_member'),
    path('<uuid:room_id>/delete/',                    views.DeleteRoomView.as_view(),       name='delete_chat_room'),

    # ── Soft-delete a message (AJAX) ─────────────────────────────────────────
    path('api/delete/<uuid:message_id>/', views.DeleteMessageView.as_view(), name='delete_chat_message'),

    # ── Files-app picker ─────────────────────────────────────────────────────
    path('<uuid:room_id>/files/',        views.ChatFilePickerAPIView.as_view(),    name='chat_file_picker'),
    path('<uuid:room_id>/attach-file/',  views.ChatAttachSharedFileView.as_view(), name='chat_attach_file'),
]