import os
from django.core.asgi import get_asgi_application
from channels.routing import ProtocolTypeRouter, URLRouter
from channels.security.websocket import AllowedHostsOriginValidator, OriginValidator

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'easyoffice.settings')

# Must come BEFORE imports that touch Django models / settings.
django_asgi_app = get_asgi_application()

from apps.messaging import routing as messaging_routing
from apps.notifications_ws import routing as notification_routing
from apps.files import routing as files_routing
from apps.mobile_api.ws_auth import JWTAuthMiddlewareStack

application = ProtocolTypeRouter({
    'http': django_asgi_app,
    'websocket': AllowedHostsOriginValidator(
        JWTAuthMiddlewareStack(
            URLRouter(
                messaging_routing.websocket_urlpatterns +
                notification_routing.websocket_urlpatterns +
                files_routing.websocket_urlpatterns
            )
        )
    ),
})