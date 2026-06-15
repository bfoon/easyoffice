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
#
# Previously these routes were (a) not registered here at all — so the
# socket matched nothing and the page sat on "Connecting…" forever — and
# (b) would have been wrapped in the JWT-only stack, which leaves a
# cookie-based browser socket anonymous and gets the agent rejected with
# 'unauthenticated'. Hence the dedicated session-auth router below.
cs_websockets = AuthMiddlewareStack(
    URLRouter(customer_service_routing.websocket_urlpatterns)
)

# ── Existing JWT-authenticated sockets (mobile API, messaging, files) ─────
# These clients send a bearer token, so they keep the JWT stack untouched.
jwt_websockets = JWTAuthMiddlewareStack(
    URLRouter(
        messaging_routing.websocket_urlpatterns +
        notification_routing.websocket_urlpatterns +
        files_routing.websocket_urlpatterns
    )
)


async def websocket_router(scope, receive, send):
    """
    Dispatch by path: customer-service live-chat sockets use session auth,
    everything else uses the JWT stack. Done at the ASGI level because the
    two route groups need *different* auth middleware, which a single
    URLRouter can't express.
    """
    path = scope.get('path', '') or ''
    if path.startswith('/ws/cs/live-chat/'):
        return await cs_websockets(scope, receive, send)
    return await jwt_websockets(scope, receive, send)


application = ProtocolTypeRouter({
    'http': django_asgi_app,
    'websocket': websocket_router,
})