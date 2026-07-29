"""
apps/messaging/ws_auth.py
─────────────────────────
JWT authentication middleware for Django Channels WebSockets.

🔒 SECURITY CHANGES vs the previous version
-------------------------------------------
1.  Tokens are now read from the ``Authorization: Bearer <jwt>`` header
    first. The old query-string method (?token=...) leaked valid access
    tokens into nginx/proxy access logs, browser history, and any
    request-logging middleware, because query strings are logged in
    plain text everywhere.

    * Native mobile apps CAN set headers on WebSocket connections —
      switch the app to the Authorization header.
    * Browsers CANNOT set custom WS headers; browser clients should use
      session-cookie auth (wrap this middleware around Channels'
      AuthMiddlewareStack — see asgi.py note below).

2.  The query-string fallback is kept for backward compatibility during
    the mobile-app migration, but it is now gated behind the setting
    ``MESSAGING_WS_ALLOW_QUERY_TOKEN``, which now DEFAULTS TO FALSE. It
    used to default to True, which meant the leak was on unless somebody
    thought to turn it off. Set it to True only for as long as the mobile
    rollout needs it; every use logs a warning.

3.  ``scope["user"]`` is now ALWAYS defined after this middleware runs.
    Previously, when no token was supplied, the key was left unset and
    the consumer's ``self.scope['user']`` raised KeyError unless another
    auth middleware happened to run first.

asgi.py wiring (recommended order — session auth first, JWT overrides):

    from channels.auth import AuthMiddlewareStack
    from apps.messaging.ws_auth import JWTAuthMiddleware

    application = ProtocolTypeRouter({
        "websocket": JWTAuthMiddleware(
            AuthMiddlewareStack(
                URLRouter(websocket_urlpatterns)
            )
        ),
    })
"""

import logging
from urllib.parse import parse_qs

from channels.db import database_sync_to_async
from django.conf import settings
from django.contrib.auth.models import AnonymousUser

logger = logging.getLogger(__name__)


@database_sync_to_async
def _user_from_token(token):
    from rest_framework_simplejwt.tokens import AccessToken
    from apps.core.models import User
    try:
        data = AccessToken(token)  # verifies signature + expiry + type
        return User.objects.get(id=data["user_id"], is_active=True)
    except Exception:
        # Invalid/expired token or inactive user — treat as anonymous.
        return AnonymousUser()


def _token_from_headers(scope):
    """Extract a Bearer token from the Authorization header, if present."""
    for name, value in scope.get("headers", []):
        if name == b"authorization":
            try:
                parts = value.decode("utf-8").split()
            except Exception:
                return None
            if len(parts) == 2 and parts[0].lower() == "bearer":
                return parts[1]
    return None


def _token_from_query(scope):
    """DEPRECATED fallback: ?token=<jwt>. Leaks tokens into access logs."""
    qs = parse_qs(scope.get("query_string", b"").decode())
    return (qs.get("token") or [None])[0]


def _scope_is_secure(scope):
    """
    True when this WebSocket arrived over TLS.

    Channels sets scope["scheme"] to "wss" for a direct TLS connection. When
    nginx terminates TLS the scheme is plain "ws", so we also honour the
    X-Forwarded-Proto header the proxy sets. Localhost is always treated as
    secure so development is not blocked.
    """
    if scope.get("scheme") in ("wss", "https"):
        return True

    for name, value in scope.get("headers", []):
        if name == b"x-forwarded-proto":
            try:
                proto = value.decode("ascii").split(",")[0].strip().lower()
            except Exception:
                continue
            if proto in ("https", "wss"):
                return True

    host = (scope.get("server") or ("", 0))[0] or ""
    if host in ("127.0.0.1", "::1", "localhost"):
        return True

    for name, value in scope.get("headers", []):
        if name == b"host":
            try:
                h = value.decode("ascii").split(":")[0].lower()
            except Exception:
                continue
            if h in ("localhost", "127.0.0.1", "::1"):
                return True

    return False


class JWTAuthMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        token = _token_from_headers(scope)

        if not token and getattr(settings, "MESSAGING_WS_ALLOW_QUERY_TOKEN", False):
            token = _token_from_query(scope)
            if token:
                logger.warning(
                    "WebSocket auth via ?token= query string is deprecated and "
                    "leaks tokens into access logs. Move the client to the "
                    "'Authorization: Bearer' header, then set "
                    "MESSAGING_WS_ALLOW_QUERY_TOKEN = False."
                )

        # 4.  Refuse to authenticate a socket that is not on TLS. A ws://
        #     connection carries the token, every message, and every call
        #     signal in clear text. Django itself cannot see the scheme
        #     behind a proxy, so we trust the same forwarded header the
        #     rest of the stack uses.
        if token and not _scope_is_secure(scope) and not getattr(settings, "MESSAGING_WS_ALLOW_INSECURE", False):
            logger.error(
                "Refusing WebSocket authentication over a non-TLS connection. "
                "Messages and tokens would travel in clear text. Serve the "
                "socket as wss:// (see the nginx snippet in "
                "docs/transport-security.md), or set MESSAGING_WS_ALLOW_INSECURE "
                "= True for local development only."
            )
            token = None

        if token:
            scope["user"] = await _user_from_token(token)
        elif "user" not in scope:
            # Guarantee the key exists so consumers never KeyError.
            # (If AuthMiddlewareStack ran first, it already set a
            # session-authenticated user and we leave it alone.)
            scope["user"] = AnonymousUser()

        return await self.app(scope, receive, send)
