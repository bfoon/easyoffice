"""
apps/customer_service/routing.py
────────────────────────────────
WebSocket URL routing for live chat. Imported by your project-level
asgi.py — see the "Wire-up" section in the deployment notes.
"""
from django.urls import re_path

from . import consumers

# UUID pattern, case-insensitive 8-4-4-4-12 hex. We allow any of the
# common stringifications.
_UUID_RE = r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}'

websocket_urlpatterns = [
    # Customer side — tokenised, anonymous.
    re_path(
        rf'^ws/cs/live-chat/customer/(?P<token>{_UUID_RE})/$',
        consumers.LiveChatConsumer.as_asgi(),
    ),
    # Agent side — ticket pk, authenticated.
    re_path(
        r'^ws/cs/live-chat/agent/(?P<ticket_pk>\d+)/$',
        consumers.LiveChatConsumer.as_asgi(),
    ),
]
