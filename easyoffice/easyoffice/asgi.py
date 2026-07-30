import os

from django.core.asgi import get_asgi_application
from channels.routing import ProtocolTypeRouter, URLRouter
from channels.security.websocket import AllowedHostsOriginValidator

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'easyoffice.settings')

# Must come BEFORE imports that touch Django models / settings.
django_asgi_app = get_asgi_application()

from channels.auth import AuthMiddlewareStack

from apps.messaging import routing as messaging_routing
from apps.notifications_ws import routing as notification_routing
from apps.files import routing as files_routing
from apps.customer_service import routing as customer_service_routing
from apps.mobile_api.ws_auth import JWTAuthMiddlewareStack


# ── Customer-service live chat (SESSION auth) ─────────────────────────────
# Both ends are ordinary browser WebSockets:
#   * the agent panel runs in a logged-in staff session, so it needs
#     Django SESSION auth — AuthMiddlewareStack reads the session cookie
#     to populate scope['user'].
#   * the customer token page is anonymous and identifies itself by the
#     UUID token + machine cookie; AuthMiddlewareStack is harmless there
#     (scope['user'] just stays AnonymousUser, which the consumer allows).
cs_websockets = AuthMiddlewareStack(
    URLRouter(customer_service_routing.websocket_urlpatterns)
)

# ── Shared routes: same paths, two kinds of client ────────────────────────
# Chat, notifications and file sockets are opened by BOTH the mobile app
# (bearer token) and the browser (session cookie). The browser cannot send
# an Authorization header — the WebSocket constructor accepts a URL and a
# subprotocol list, nothing else — so a JWT-only stack leaves every browser
# socket as AnonymousUser and the consumer closes it with 403. That is the
# "403 0" on /ws/chat/<uuid>/ retrying once a second in the nginx log.
#
# Build the same URLRouter twice, under each stack, and pick per connection.
_shared_patterns = (
    messaging_routing.websocket_urlpatterns +
    notification_routing.websocket_urlpatterns +
    files_routing.websocket_urlpatterns
)

session_websockets = AuthMiddlewareStack(URLRouter(_shared_patterns))
jwt_websockets     = JWTAuthMiddlewareStack(URLRouter(_shared_patterns))


def _carries_bearer_token(scope):
    """
    True when the client identified itself the mobile way. Checked in the
    order the clients actually use:
      1. ?token=<jwt> on the query string
      2. Sec-WebSocket-Protocol carrying the token as a subprotocol
      3. an Authorization header (native clients, not browsers)
    Anything else is treated as a cookie-authenticated browser socket.
    """
    qs = (scope.get('query_string') or b'').decode('utf-8', 'ignore')
    if 'token=' in qs or 'jwt=' in qs:
        return True

    for name, value in scope.get('headers') or []:
        if name == b'authorization':
            return True
        if name == b'sec-websocket-protocol':
            protos = value.decode('utf-8', 'ignore').lower()
            if 'bearer' in protos or 'jwt' in protos or 'access_token' in protos:
                return True
    return False


async def websocket_router(scope, receive, send):
    """
    Dispatch by path, then by credential type:
      * /ws/cs/live-chat/  → session auth (agent panel + anonymous customer)
      * everything else    → JWT if the client presented a token,
                             session cookie otherwise.
    Done at the ASGI level because these route groups need *different* auth
    middleware, which a single URLRouter cannot express.
    """
    path = scope.get('path', '') or ''
    if path.startswith('/ws/cs/live-chat/'):
        return await cs_websockets(scope, receive, send)
    if _carries_bearer_token(scope):
        return await jwt_websockets(scope, receive, send)
    return await session_websockets(scope, receive, send)


application = ProtocolTypeRouter({
    'http': django_asgi_app,
    'websocket': AllowedHostsOriginValidator(websocket_router),
})