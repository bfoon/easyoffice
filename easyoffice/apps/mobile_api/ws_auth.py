"""
apps/mobile_api/ws_auth.py
──────────────────────────
WebSocket auth middleware that accepts a JWT in the query string.

Usage in asgi.py:

    from apps.mobile_api.ws_auth import JWTAuthMiddlewareStack

    application = ProtocolTypeRouter({
        "http": django_asgi_app,
        "websocket": JWTAuthMiddlewareStack(URLRouter(websocket_urlpatterns)),
    })

The mobile app connects with:
    wss://easyoffice.example.com/ws/chat/<room_id>/?token=<access_jwt>

If no `?token=` is supplied (browser session-cookie path), this middleware
falls back to AuthMiddlewareStack so existing web flows are unaffected.
"""

from urllib.parse import parse_qs

from channels.auth import AuthMiddlewareStack
from channels.db import database_sync_to_async
from channels.middleware import BaseMiddleware
from django.contrib.auth.models import AnonymousUser


@database_sync_to_async
def _get_user_from_jwt(raw_token: str):
    """Validate the JWT and return the matching User (or AnonymousUser)."""
    try:
        from rest_framework_simplejwt.tokens import UntypedToken
        from rest_framework_simplejwt.exceptions import InvalidToken, TokenError
        from django.conf import settings
        import jwt

        # Validate signature + expiry (will raise on failure)
        UntypedToken(raw_token)

        decoded = jwt.decode(
            raw_token,
            settings.SECRET_KEY,
            algorithms=[
                getattr(settings, 'SIMPLE_JWT', {}).get('ALGORITHM', 'HS256')
            ],
        )
        user_id = decoded.get('user_id')
        if not user_id:
            return AnonymousUser()

        from apps.core.models import User
        try:
            return User.objects.get(id=user_id, is_active=True)
        except User.DoesNotExist:
            return AnonymousUser()
    except Exception:
        return AnonymousUser()


class JWTAuthMiddleware(BaseMiddleware):
    """
    Pull a JWT out of `?token=...` and resolve it to a Django user before
    delegating to the inner ASGI app. Falls back to the existing
    session-based user from AuthMiddlewareStack when no token is given.
    """

    async def __call__(self, scope, receive, send):
        # Already authenticated by the outer AuthMiddlewareStack?
        existing_user = scope.get('user')

        token = None
        try:
            qs = parse_qs((scope.get('query_string') or b'').decode('utf-8'))
            token_list = qs.get('token') or qs.get('access_token')
            if token_list:
                token = token_list[0]
        except Exception:
            token = None

        if token:
            user = await _get_user_from_jwt(token)
            scope['user'] = user
        elif existing_user is None:
            scope['user'] = AnonymousUser()
        # else: keep the session user that AuthMiddlewareStack already set.

        return await super().__call__(scope, receive, send)


def JWTAuthMiddlewareStack(inner):
    """
    Compose JWT-from-query-string check on top of the standard
    session-based auth stack. Order matters: AuthMiddlewareStack runs
    first so scope['user'] is populated from the session, then we
    *override* with a JWT-resolved user if one is supplied.
    """
    return JWTAuthMiddleware(AuthMiddlewareStack(inner))
