from django.urls import path
from apps.messaging import views

urlpatterns = [
    path('', views.ChatListView.as_view(), name='chat_list'),
    path('create/', views.CreateChatRoomView.as_view(), name='create_chat_room'),
    path('dm/<uuid:user_id>/', views.DirectMessageView.as_view(), name='direct_message'),

    path('<uuid:room_id>/', views.ChatRoomView.as_view(), name='chat_room'),
    path('<uuid:room_id>/add-members/', views.AddRoomMemberView.as_view(), name='add_room_members'),
    path('<uuid:room_id>/remove-member/<uuid:user_id>/', views.RemoveRoomMemberView.as_view(), name='remove_room_member'),
    path('<uuid:room_id>/delete/', views.DeleteRoomView.as_view(), name='delete_chat_room'),
]