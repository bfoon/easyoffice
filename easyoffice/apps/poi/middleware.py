"""
apps/poi/middleware.py
──────────────────────
POIConfinementMiddleware — the single global enforcement point that keeps a
Person-Of-Interest confined to the POI portal.

A POI is a real core.User with a usable password, so without this guard a POI
who reached the main login (or obtained a main-app session by any other means)
could browse EasyOffice. This middleware makes that impossible: a logged-in POI
may ONLY touch POI-portal URLs plus a tiny allowlist (logout, static, media).
Anything else is redirected to the portal.

The rule fails CLOSED: any path not explicitly allowed is denied for a POI.

Register in settings.py AFTER AuthenticationMiddleware (so request.user is set),
and after ActiveUserMiddleware is fine too:

    MIDDLEWARE = [
        ...
        'django.contrib.auth.middleware.AuthenticationMiddleware',
        'apps.core.middleware.ActiveUserMiddleware',
        ...
        'apps.poi.middleware.POIConfinementMiddleware',
    ]
"""

from django.shortcuts import redirect


class POIConfinementMiddleware:
    # Path prefixes a logged-in POI is permitted to reach. Everything else is
    # denied for a POI. Prefix-based so it fails closed.
    ALLOWED_PREFIXES = (
        '/poi/',        # the entire POI portal: pages, API, POI auth
        '/static/',
        '/media/',
    )

    # Exact main-app paths a POI may still hit — chiefly to log out a stray
    # main-app session. Adjust to your real logout path if it differs.
    ALLOWED_EXACT = (
        '/logout/',
        '/accounts/logout/',
    )

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, 'user', None)
        if (
            user is not None
            and user.is_authenticated
            and getattr(user, 'poi_profile', None) is not None
        ):
            if not self._poi_allowed(request.path or ''):
                return redirect('poi_portal')

        return self.get_response(request)

    def _poi_allowed(self, path):
        if path in self.ALLOWED_EXACT:
            return True
        return path.startswith(self.ALLOWED_PREFIXES)