import os

from django.core.asgi import get_asgi_application
from django.conf import settings
from channels.routing import ProtocolTypeRouter, URLRouter
from channels.security.websocket import OriginValidator

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'easyoffice.settings')

# Must come BEFORE imports that touch Django models / settings.
django_asgi_app = get_asgi_application()

from channels.auth import AuthMiddlewareStack

from apps.messaging import routing as messaging_routing
from apps.notifications_ws import routing as notification_routing
from apps.files import routing as files_routing
from apps.customer_service import routing as customer_service_routing

# NOTE: apps.messaging.ws_auth, NOT apps.mobile_api.ws_auth. The latter is
# the older JWT-only middleware that leaves scope['user'] unset when no
# token is supplied — which is what made ChatConsumer.connect() close every
# browser socket with 403.
from apps.messaging.ws_auth import JWTAuthMiddleware


# ── Origin policy ──────────────────────────────────────────────────────────
# Browsers always send an Origin header and cannot forge it, so validating
# it still blocks cross-site socket hijacking — the entire point of this
# validator, and worth keeping.
#
# Native clients send no Origin at all, and OriginValidator.valid_origin()
# rejects a None origin unless the allowed list literally contains "*".
# That is why the Flutter app was refused upstream of any auth middleware.
# A null Origin is only reachable by a non-browser client, and those
# sockets are authenticated by their bearer token, so allowing it is safe.
#
# Subclass OriginValidator, NOT AllowedHostsOriginValidator: in this version
# of Channels the latter is a factory FUNCTION that returns an
# OriginValidator, and subclassing a function raises
# "TypeError: function() argument 'code' must be code, not str" at import.
class AppOriginValidator(OriginValidator):
    def valid_origin(self, parsed_origin):
        if parsed_origin is None:
            return True
        return super().valid_origin(parsed_origin)


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

# ── Shared routes: browser (session cookie) AND mobile (bearer token) ─────
# The nesting order IS the fix, and it is not interchangeable:
#
#   AuthMiddlewareStack runs first and resolves scope['user'] from the
#   session cookie. JWTAuthMiddleware then runs and overrides it only when
#   a token is present — its `elif "user" not in scope` guard leaves an
#   already-resolved session user untouched.
#
# Reversing this (JWT outermost) silently breaks mobile instead: Channels'
# AuthMiddleware assigns scope['user'] unconditionally, so it would clobber
# every JWT-resolved user with AnonymousUser.
shared_websockets = AuthMiddlewareStack(
    JWTAuthMiddleware(
        URLRouter(
            messaging_routing.websocket_urlpatterns +
            notification_routing.websocket_urlpatterns +
            files_routing.websocket_urlpatterns
        )
    )
)


async def websocket_router(scope, receive, send):
    """
    Dispatch by path. Customer-service live chat is session-only; every
    other route accepts either a session cookie or a bearer token, resolved
    by the middleware nesting above rather than by inspecting the request.
    """
    path = scope.get('path', '') or ''
    if path.startswith('/ws/cs/live-chat/'):
        return await cs_websockets(scope, receive, send)
    return await shared_websockets(scope, receive, send)


application = ProtocolTypeRouter({
    'http': django_asgi_app,
    'websocket': AppOriginValidator(websocket_router, settings.ALLOWED_HOSTS),
})