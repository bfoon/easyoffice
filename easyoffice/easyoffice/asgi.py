import os
from django.core.asgi import get_asgi_application
from channels.routing import ProtocolTypeRouter, URLRouter
from channels.auth import AuthMiddlewareStack
from channels.security.websocket import AllowedHostsOriginValidator

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'easyoffice.settings')

django_asgi_app = get_asgi_application()

from apps.messaging import routing as messaging_routing
from apps.notifications_ws import routing as notification_routing

application = ProtocolTypeRouter({
    'http': django_asgi_app,
    'websocket': AllowedHostsOriginValidator(
        AuthMiddlewareStack(
            URLRouter(
                messaging_routing.websocket_urlpatterns +
                notification_routing.websocket_urlpatterns
            )
        )
    ),
})
