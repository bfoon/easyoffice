"""
apps/files/collabora/routing.py

WebSocket URL routing for presence updates. Include this from your
project-level asgi.py via `URLRouter`.
"""
from django.urls import path
from .consumers import CollaboraPresenceConsumer


websocket_urlpatterns = [
    path(
        'ws/files/<uuid:file_id>/presence/',
        CollaboraPresenceConsumer.as_asgi(),
        name='collabora_presence_ws',
    ),
]
