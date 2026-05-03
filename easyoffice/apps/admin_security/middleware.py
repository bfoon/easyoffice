"""
apps/admin_security/middleware.py
─────────────────────────────────
Two responsibilities:

1.  AdminKillSwitchMiddleware
        If `AdminAccessSetting.is_admin_enabled` is False, every URL under
        /admin/ returns 503-style "admin is disabled" — EXCEPT for the
        superuser who flipped the switch (we always honour that user so
        you can never lock yourself out of your own site).

2.  AdminOTPGateMiddleware
        If `require_otp_for_admin` is True, after a user submits a valid
        password to /admin/login/ we redirect them to our OTP challenge
        page. They aren't considered "admin-authenticated" until the OTP
        is verified — even though Django's session already says they're
        logged in.

Order in MIDDLEWARE matters — both must come AFTER AuthenticationMiddleware:

    MIDDLEWARE = [
        ...
        'django.contrib.sessions.middleware.SessionMiddleware',
        'django.contrib.auth.middleware.AuthenticationMiddleware',
        'apps.admin_security.middleware.AdminKillSwitchMiddleware',
        'apps.admin_security.middleware.AdminOTPGateMiddleware',
        ...
    ]
"""
from __future__ import annotations

from django.conf import settings
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.urls import resolve, reverse, NoReverseMatch
from django.utils.deprecation import MiddlewareMixin

from .models import AdminAccessSetting

# Session key set by our OTP view once the user has verified.
OTP_VERIFIED_SESSION_KEY = '_admin_otp_verified_user_id'

# Paths that the OTP gate must NEVER block — otherwise the user can never
# reach the page that lets them complete the challenge.
OTP_ALLOWLIST_PATHS = (
    '/admin/login/',          # password step — happens before OTP
    '/admin/logout/',         # logging out is always allowed
    '/admin-security/',       # our own OTP + settings views live here
    '/static/',
    '/media/',
)


def _is_admin_path(path: str) -> bool:
    """
    True if the path lives under the Django admin. We honour the project's
    actual admin URL prefix if it's not the default /admin/.
    """
    prefix = getattr(settings, 'ADMIN_URL_PREFIX', '/admin/')
    if not prefix.startswith('/'):
        prefix = '/' + prefix
    if not prefix.endswith('/'):
        prefix += '/'
    return path == prefix.rstrip('/') or path.startswith(prefix)


def _is_allowlisted(path: str) -> bool:
    return any(path.startswith(p) for p in OTP_ALLOWLIST_PATHS)


# ── Kill-switch ────────────────────────────────────────────────────────────

class AdminKillSwitchMiddleware(MiddlewareMixin):
    """
    Blocks the admin when AdminAccessSetting.is_admin_enabled is False.

    Bypass rules (in order):
        • The superuser who last toggled the setting always gets through.
        • Any other authenticated superuser hitting the admin gets
          a 'disabled' page with a single 'Re-enable' button taking them
          to our settings page.
        • Everyone else gets the same disabled page WITHOUT the button.
    """

    def process_request(self, request):
        if not _is_admin_path(request.path):
            return None

        try:
            setting = AdminAccessSetting.load()
        except Exception:
            # DB not ready (migrations not run yet etc.) — fail open.
            return None

        if setting.is_admin_enabled:
            return None

        user = getattr(request, 'user', None)
        is_super = bool(user and user.is_authenticated and user.is_superuser)

        # The superuser who flipped the switch always gets in.
        if is_super and setting.last_changed_by_id and user.pk == setting.last_changed_by_id:
            return None

        try:
            settings_url = reverse('admin_security:settings')
        except NoReverseMatch:
            settings_url = ''

        ctx = {
            'is_super': is_super,
            'settings_url': settings_url,
        }
        response = render(request, 'admin_security/admin_disabled.html', ctx, status=503)
        response['Retry-After'] = '300'
        return response


# ── OTP gate ───────────────────────────────────────────────────────────────

class AdminOTPGateMiddleware(MiddlewareMixin):
    """
    For admin paths, if require_otp_for_admin is True and the current user
    has not yet verified their OTP for this session, redirect them to the
    OTP challenge view. The flag is keyed by user-id so re-logging in as a
    different account always re-prompts.
    """

    def process_request(self, request):
        if not _is_admin_path(request.path):
            return None
        if _is_allowlisted(request.path):
            return None

        user = getattr(request, 'user', None)
        if not user or not user.is_authenticated:
            return None  # Django's own login_required will handle it.

        try:
            setting = AdminAccessSetting.load()
        except Exception:
            return None
        if not setting.require_otp_for_admin:
            return None

        # If we already issued + verified an OTP for this user this session,
        # let them through.
        verified_for = request.session.get(OTP_VERIFIED_SESSION_KEY)
        if verified_for == user.pk:
            return None

        try:
            url = reverse('admin_security:otp_challenge')
        except NoReverseMatch:
            return HttpResponse(
                'Admin OTP is required but the otp_challenge URL is not '
                'wired up. Add apps.admin_security.urls to your URL conf.',
                status=500,
            )
        # Preserve where they were trying to go.
        return redirect(f'{url}?next={request.get_full_path()}')
