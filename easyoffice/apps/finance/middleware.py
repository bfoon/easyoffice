"""
apps/finance/middleware.py
===========================

Thread-local stash for the current request and authenticated actor.

Django signals don't have access to the request that triggered them, so we
park the request and user on a thread-local in middleware and let signal
handlers reach in via get_current_actor() / get_current_request().

Add to settings.MIDDLEWARE *after* AuthenticationMiddleware:

    MIDDLEWARE = [
        ...
        'django.contrib.auth.middleware.AuthenticationMiddleware',
        'apps.finance.middleware.FinanceAuditMiddleware',
        ...
    ]
"""
import threading

_local = threading.local()


class FinanceAuditMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        _local.request = request
        _local.actor = (
            request.user if getattr(request, 'user', None) and request.user.is_authenticated else None
        )
        try:
            return self.get_response(request)
        finally:
            # Clear so worker threads don't leak request state across tasks
            _local.request = None
            _local.actor = None


def get_current_actor():
    """Return the User who triggered the current request, or None."""
    return getattr(_local, 'actor', None)


def get_current_request():
    """Return the current request, or None (e.g. for management commands)."""
    return getattr(_local, 'request', None)