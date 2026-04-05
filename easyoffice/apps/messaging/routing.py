from django.urls import re_path
from apps.messaging import consumers

websocket_urlpatterns = [
    re_path(r'ws/chat/(?P<room_id>[^/]+)/$', consumers.ChatConsumer.as_asgi()),
]
