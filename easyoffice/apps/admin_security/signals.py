"""
apps/admin_security/signals.py
──────────────────────────────
When an admin/staff user finishes the password step on /admin/login/,
issue an OTP and email it. The middleware then catches the next admin
request and forwards them to the challenge page where the code is waiting.

We also clear the verified flag on user_logged_out so subsequent
logins always re-prompt.
"""
from __future__ import annotations

import logging

from django.contrib.auth.signals import user_logged_in, user_logged_out
from django.dispatch import receiver

from .middleware import OTP_VERIFIED_SESSION_KEY
from .models import AdminAccessSetting

logger = logging.getLogger(__name__)


def _is_admin_login(request) -> bool:
    """Heuristic: we only auto-issue OTPs for admin-area logins."""
    if request is None:
        return False
    path = getattr(request, 'path', '') or ''
    referer = request.META.get('HTTP_REFERER', '') if hasattr(request, 'META') else ''
    return '/admin/' in path or '/admin/' in referer


@receiver(user_logged_in)
def issue_admin_otp_on_login(sender, request, user, **kwargs):
    if not user or not (user.is_staff or user.is_superuser):
        return
    if not _is_admin_login(request):
        return

    try:
        if not AdminAccessSetting.load().require_otp_for_admin:
            return
    except Exception:
        return

    # Imported lazily so signals.py can be imported during app loading
    # without dragging in views (which import templates / mail backends).
    from . import views

    # Wipe any prior 'verified' flag — they need to re-verify each login.
    if hasattr(request, 'session'):
        request.session.pop(OTP_VERIFIED_SESSION_KEY, None)

    try:
        views.send_admin_otp(user, request)
    except Exception:
        logger.exception('Failed to issue admin OTP on login for user %s', user.pk)


@receiver(user_logged_out)
def clear_otp_flag_on_logout(sender, request, user, **kwargs):
    if request is not None and hasattr(request, 'session'):
        request.session.pop(OTP_VERIFIED_SESSION_KEY, None)
