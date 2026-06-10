"""apps/customer_service/routing.py
Import this from project-level asgi.py.
"""
from django.urls import re_path
from . import consumers

_UUID_RE = r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}'

websocket_urlpatterns = [
    re_path(rf'^ws/cs/live-chat/customer/(?P<token>{_UUID_RE})/$', consumers.LiveChatConsumer.as_asgi()),
    re_path(rf'^ws/cs/live-chat/portal/(?P<portal_token>{_UUID_RE})/$', consumers.LiveChatConsumer.as_asgi()),
    re_path(r'^ws/cs/live-chat/agent/(?P<ticket_pk>\d+)/$', consumers.LiveChatConsumer.as_asgi()),
]