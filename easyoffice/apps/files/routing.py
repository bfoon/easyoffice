from django.urls import re_path
from apps.files.consumers import LivePreviewConsumer

websocket_urlpatterns = [
    re_path(
        r'^ws/files/live-preview/(?P<session_token>[0-9a-f-]+)/$',
        LivePreviewConsumer.as_asgi()
    ),
]