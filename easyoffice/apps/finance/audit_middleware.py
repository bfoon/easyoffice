"""
apps/finance/audit_middleware.py
=================================

Thread-local stash of the current request, so signal handlers can find out
who is acting without having to plumb `request` through every model save.

Add this to MIDDLEWARE in settings.py, **after** AuthenticationMiddleware:

    MIDDLEWARE = [
        ...
        'django.contrib.auth.middleware.AuthenticationMiddleware',
        ...
        'apps.finance.audit_middleware.FinanceAuditMiddleware',
    ]
"""
import threading

_local = threading.local()


def get_current_request():
    """Return the request bound to this thread, or None outside a request."""
    return getattr(_local, 'request', None)


def get_current_user():
    req = get_current_request()
    return getattr(req, 'user', None) if req else None


class FinanceAuditMiddleware:
    """
    Stores the current request in thread-local so finance signal handlers
    can pull the actor + IP without an explicit argument.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        _local.request = request
        try:
            return self.get_response(request)
        finally:
            # Don't leak across threads / async workers
            try:
                del _local.request
            except AttributeError:
                pass
