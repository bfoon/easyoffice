"""
apps/files/collabora/routing.py

WebSocket URL routing for presence updates. Include this from your
project-level asgi.py via `URLRouter`.
"""
from django.urls import path
from .consumers import CollaboraPresenceConsumer
from django.urls import re_path
from apps.files.consumers import LivePreviewConsumer



websocket_urlpatterns = [
    path(
        'ws/files/<uuid:file_id>/presence/',
        CollaboraPresenceConsumer.as_asgi(),
        name='collabora_presence_ws',
    ),
re_path(r'^ws/files/live-preview/(?P<session_token>[0-9a-f-]+)/$', LivePreviewConsumer.as_asgi()),
]
