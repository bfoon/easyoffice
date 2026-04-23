"""
apps.user_admin.middleware
──────────────────────────
PasswordChangeRequiredMiddleware

When an admin issues a temp password, TempPassword.is_consumed=False
until the user changes their password. This middleware intercepts every
authenticated request and redirects the user to /change-password/ until
they do — except for a small allow-list of URLs the user needs to
navigate to in order to comply.

Wire in settings.py:

    MIDDLEWARE = [
        ...
        'django.contrib.auth.middleware.AuthenticationMiddleware',
        ...
        'apps.user_admin.middleware.PasswordChangeRequiredMiddleware',
        ...
    ]

Place it AFTER AuthenticationMiddleware so request.user is populated.
"""
from django.conf import settings
from django.http import HttpResponseRedirect
from django.urls import reverse, NoReverseMatch

from apps.user_admin.models import TempPassword


def _safe_reverse(name):
    try:
        return reverse(name)
    except NoReverseMatch:
        return None


class PasswordChangeRequiredMiddleware:
    """Redirect users with an outstanding temp password to /change-password/."""

    # Paths users are allowed to reach even while a temp pw is outstanding.
    # Everything else redirects to the change-password page.
    _STATIC_ALLOW_PREFIXES = (
        '/static/',
        '/media/',
        '/favicon',
        '/admin/logout',   # escape hatch
    )

    def __init__(self, get_response):
        self.get_response = get_response

        # Pre-compute URLs we must allow through so the user can comply.
        self._change_url = _safe_reverse('user_admin:force_change_password') or '/change-password/'
        # Django auth default names — may or may not be wired up in the project.
        self._logout_url = _safe_reverse('logout')
        self._login_url  = _safe_reverse('login') or getattr(settings, 'LOGIN_URL', '/accounts/login/')

    def __call__(self, request):
        user = getattr(request, 'user', None)
        if user and user.is_authenticated and TempPassword.user_must_change_password(user):
            path = request.path
            if not self._is_allowed(path):
                return HttpResponseRedirect(self._change_url)
        return self.get_response(request)

    def _is_allowed(self, path):
        if path == self._change_url:
            return True
        if self._logout_url and path == self._logout_url:
            return True
        if self._login_url and path == self._login_url:
            return True
        for prefix in self._STATIC_ALLOW_PREFIXES:
            if path.startswith(prefix):
                return True
        return False
