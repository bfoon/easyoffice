from django.urls import path
from rest_framework_simplejwt.views import TokenRefreshView

from apps.mobile_api import views

app_name = 'mobile_api'

urlpatterns = [
    # AUTH
    path('auth/login/',     views.MobileLoginView.as_view(),  name='login'),
    path('auth/refresh/',   TokenRefreshView.as_view(),       name='refresh'),
    path('auth/logout/',    views.MobileLogoutView.as_view(), name='logout'),
    path('auth/me/',        views.MobileMeView.as_view(),     name='me'),

    # ROOMS
    path('rooms/',                              views.RoomListView.as_view(),    name='rooms'),
    path('rooms/direct/',                       views.DirectRoomView.as_view(),  name='direct_room'),
    path('rooms/<uuid:room_id>/',               views.RoomDetailView.as_view(),  name='room_detail'),
    path('rooms/<uuid:room_id>/messages/',      views.RoomMessagesView.as_view(),name='room_messages'),
    path('rooms/<uuid:room_id>/polls/',         views.CreatePollView.as_view(),  name='create_poll'),

    # MESSAGES
    path('messages/<uuid:message_id>/',         views.MessageDeleteView.as_view(),   name='message_delete'),
    path('messages/<uuid:message_id>/react/',   views.ReactionToggleView.as_view(),  name='message_react'),

    # POLLS
    path('polls/<uuid:poll_id>/vote/',          views.VotePollView.as_view(),    name='poll_vote'),

    # PRESENCE
    path('presence/heartbeat/',                 views.PresenceHeartbeatView.as_view(), name='presence_hb'),
    path('presence/<uuid:user_id>/',            views.PresenceQueryView.as_view(),     name='presence'),

    # USERS
    path('users/search/',                       views.UserSearchView.as_view(),  name='user_search'),

    # DEVICE TOKENS
    path('device-tokens/',                      views.DeviceTokenView.as_view(), name='device_tokens'),
]
