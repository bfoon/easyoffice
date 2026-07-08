from django.urls import re_path
from apps.messaging import consumers

websocket_urlpatterns = [
    # 🔒 SECURITY: only accept well-formed UUIDs. The old pattern
    # ([^/]+) let arbitrary strings reach ChatRoom.objects.filter(id=...),
    # which raises ValidationError (500 noise) on non-UUID input.
    re_path(
        r'ws/chat/(?P<room_id>[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})/$',
        consumers.ChatConsumer.as_asgi(),
    ),
]
