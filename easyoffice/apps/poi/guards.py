"""
apps/poi/guards.py
──────────────────
Access control for the POI portal.

POIRequiredMixin — the gate every portal view extends. On each request it:
  1. requires login,
  2. requires the user to BE a POI (has poi_profile),
  3. requires profile.is_access_valid (active + within 30-day trust window),
  4. requires the request's device fingerprint to match the bound device.

Any failure → logout + redirect to the POI login with a reason. This is what
keeps a POI confined to the portal and off the rest of EasyOffice.

device_fingerprint(request) — reads the fingerprint the client sends. The POI
portal JS computes a stable fingerprint (see portal template) and sends it as
the `X-POI-Device` header on every fetch, and as a cookie for full-page loads.
"""

import logging

from django.contrib import messages
from django.contrib.auth import logout
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import JsonResponse
from django.shortcuts import redirect

log = logging.getLogger(__name__)

DEVICE_HEADER = "HTTP_X_POI_DEVICE"
DEVICE_COOKIE = "poi_device"


def device_fingerprint(request):
    """Best-effort device fingerprint from header (AJAX) or cookie (page load)."""
    return (
        request.META.get(DEVICE_HEADER, "")
        or request.COOKIES.get(DEVICE_COOKIE, "")
        or ""
    ).strip()


def get_poi_profile(user):
    if not getattr(user, "is_authenticated", False):
        return None
    return getattr(user, "poi_profile", None)


def is_poi(user):
    return get_poi_profile(user) is not None


class POIRequiredMixin(LoginRequiredMixin):
    """Confine access to valid POIs on their bound device."""

    login_url = "poi_login"

    def _reject(self, request, reason, code):
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return JsonResponse({"ok": False, "error": reason, "code": code}, status=403)
        messages.error(request, reason)
        return redirect("poi_login")

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return self._reject(request, "Please sign in.", "auth")

        profile = get_poi_profile(request.user)
        if profile is None:
            # A non-POI user hit the POI portal. Bounce them out of it.
            return self._reject(request, "Not authorised for this portal.", "not_poi")

        if not profile.is_access_valid:
            # Expired or disabled → ensure the session can't linger.
            logout(request)
            reason = (
                "Your access window has ended. Contact an administrator."
                if profile.is_expired or profile.status == profile.Status.EXPIRED
                else "Your account is disabled."
            )
            return self._reject(request, reason, "access_invalid")

        fp = device_fingerprint(request)
        if not profile.device_matches(fp):
            logout(request)
            return self._reject(
                request,
                "This device is not recognised. Please sign in again to re-verify.",
                "device_mismatch",
            )

        request.poi_profile = profile
        return super().dispatch(request, *args, **kwargs)


class AdminPOIManageMixin(LoginRequiredMixin):
    """Gate for staff-side POI management (create / disable / list).
    Admin = staff/superuser. Adjust to your real role check if you have one."""

    def dispatch(self, request, *args, **kwargs):
        u = request.user
        if not u.is_authenticated:
            return redirect("login")
        allowed = u.is_superuser or u.is_staff or _has_admin_role(u)
        if not allowed:
            if request.headers.get("X-Requested-With") == "XMLHttpRequest":
                return JsonResponse({"ok": False, "error": "Forbidden."}, status=403)
            messages.error(request, "You do not have permission to manage POIs.")
            return redirect("file_manager")
        return super().dispatch(request, *args, **kwargs)


def _has_admin_role(user):
    """‼️ RECONCILE (optional): if you have a role system (e.g. user.role ==
    'admin' or a groups check), put it here so non-superuser admins can manage
    POIs. Defaults to False so only staff/superusers qualify until you wire it."""
    role = getattr(user, "role", None)
    if role and str(role).lower() in {"admin", "administrator"}:
        return True
    try:
        return user.groups.filter(name__in=["Admins", "Administrators"]).exists()
    except Exception:
        return False
