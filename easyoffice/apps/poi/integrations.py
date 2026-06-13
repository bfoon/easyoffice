"""
apps/poi/integrations.py
────────────────────────
⭐ THE ONE FILE YOU RECONCILE AGAINST YOUR REAL apps.core CODE ⭐

Everything POI-specific is written against the small, stable interface in this
module. If a name here doesn't match your real apps.core models/helpers, fix it
HERE — not scattered across the app. The rest of the POI app imports only from
this file for anything that touches User / trusted-devices / OTP / Notification.

What you need to verify (search your apps/core/models.py + views.py):

  1. USER MODEL              — apps.core.models.User (almost certainly correct)
  2. TRUSTED DEVICE MODEL    — the model TrustedDevicesView / ResetTrustedDevices
                               operate on. Find its real class name + the field
                               that stores the device fingerprint and (if any)
                               an expiry/trusted-until field.
  3. OTP ISSUE / VERIFY      — the function(s) behind OTPVerifyView / ResendOTP.
  4. NOTIFICATION MODEL      — the model behind NotificationsView.

Each section below has a ‼️  RECONCILE marker showing exactly what to check.
Sensible defaults are provided so the app imports cleanly; adjust field names
to match your schema.
"""

from __future__ import annotations

import logging

from django.apps import apps
from django.conf import settings
from django.utils import timezone

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# 1. USER
# ─────────────────────────────────────────────────────────────────────────────
# ‼️ RECONCILE: confirm this path. You import `from apps.core.models import User`
# everywhere else, so this should already be right.
def get_user_model():
    from apps.core.models import User
    return User


# ─────────────────────────────────────────────────────────────────────────────
# 2. TRUSTED DEVICE
# ─────────────────────────────────────────────────────────────────────────────
# ‼️ RECONCILE: set these three constants to match your real trusted-device model.
#   TRUSTED_DEVICE_MODEL  — "app_label.ModelName"
#   TD_USER_FIELD         — FK field on that model pointing at User
#   TD_FINGERPRINT_FIELD  — CharField holding the device fingerprint/hash
#   TD_TRUSTED_UNTIL_FIELD— DateTimeField for expiry, or None if your model
#                           has no expiry column (we then store expiry on
#                           POIProfile.trusted_until instead — already handled).
TRUSTED_DEVICE_MODEL = getattr(
    settings, "POI_TRUSTED_DEVICE_MODEL", "core.TrustedDevice"
)
TD_USER_FIELD = getattr(settings, "POI_TD_USER_FIELD", "user")
TD_FINGERPRINT_FIELD = getattr(settings, "POI_TD_FINGERPRINT_FIELD", "device_id")
TD_TRUSTED_UNTIL_FIELD = getattr(settings, "POI_TD_TRUSTED_UNTIL_FIELD", None)


def _trusted_device_model():
    """Resolve the trusted-device model lazily (avoids circular imports)."""
    try:
        return apps.get_model(TRUSTED_DEVICE_MODEL)
    except Exception:
        log.exception(
            "POI: could not resolve TRUSTED_DEVICE_MODEL=%r. "
            "Set settings.POI_TRUSTED_DEVICE_MODEL or edit integrations.py.",
            TRUSTED_DEVICE_MODEL,
        )
        return None


def trust_device(user, fingerprint, trusted_until=None, request=None):
    """
    Bind *fingerprint* to *user* and mark it trusted.

    Reuses your existing trusted-device table so a POI device shows up in the
    same security dashboards / reset flows as everyone else. Returns the device
    row (or None if the model couldn't be resolved — caller still proceeds, the
    POIProfile.trusted_until is the authoritative gate).
    """
    Model = _trusted_device_model()
    if Model is None or not fingerprint:
        return None

    defaults = {}
    if TD_TRUSTED_UNTIL_FIELD and trusted_until is not None:
        defaults[TD_TRUSTED_UNTIL_FIELD] = trusted_until

    lookup = {TD_USER_FIELD: user, TD_FINGERPRINT_FIELD: fingerprint}
    try:
        obj, _created = Model.objects.update_or_create(defaults=defaults, **lookup)
        return obj
    except Exception:
        log.exception("POI: trust_device failed for user=%s", getattr(user, "id", "?"))
        return None


def is_device_trusted(user, fingerprint):
    """True if *fingerprint* is a known trusted device for *user* (and, if your
    model has an expiry column, not expired)."""
    Model = _trusted_device_model()
    if Model is None or not fingerprint:
        return False
    lookup = {TD_USER_FIELD: user, TD_FINGERPRINT_FIELD: fingerprint}
    try:
        qs = Model.objects.filter(**lookup)
        if TD_TRUSTED_UNTIL_FIELD:
            qs = qs.filter(**{f"{TD_TRUSTED_UNTIL_FIELD}__gte": timezone.now()})
        return qs.exists()
    except Exception:
        log.exception("POI: is_device_trusted failed")
        return False


def untrust_all_devices(user):
    """Remove every trusted device for *user* (used at 30-day expiry)."""
    Model = _trusted_device_model()
    if Model is None:
        return 0
    try:
        deleted, _ = Model.objects.filter(**{TD_USER_FIELD: user}).delete()
        return deleted
    except Exception:
        log.exception("POI: untrust_all_devices failed")
        return 0


# ─────────────────────────────────────────────────────────────────────────────
# 3. OTP
# ─────────────────────────────────────────────────────────────────────────────
# ‼️ RECONCILE: point these at your real OTP issue/verify code (behind
# OTPVerifyView / ResendOTPView). Defaults implement a self-contained OTP using
# Django's cache so the app works out of the box; swap to your code path to keep
# a single OTP system across the product.
def issue_otp(user, request=None):
    """
    Generate + send an OTP to *user*. Return True on success.

    DEFAULT: 6-digit code in cache (10 min TTL) emailed to the user.
    Replace the body with a call into your existing OTP issuance if you have one.
    """
    try:
        from apps.core.otp import issue_otp as _core_issue_otp  # type: ignore
        return _core_issue_otp(user, request=request)
    except Exception:
        pass  # fall back to built-in below

    import random
    from django.core.cache import cache
    from django.core.mail import send_mail

    code = f"{random.randint(0, 999999):06d}"
    cache.set(f"poi-otp:{user.pk}", code, timeout=600)
    try:
        send_mail(
            subject="Your verification code",
            message=f"Your one-time code is {code}. It expires in 10 minutes.",
            from_email=getattr(settings, "DEFAULT_FROM_EMAIL", None),
            recipient_list=[user.email],
            fail_silently=True,
        )
    except Exception:
        log.exception("POI: failed to send OTP email")
    return True


def verify_otp(user, code, request=None):
    """Validate *code* for *user*. Return True if correct."""
    try:
        from apps.core.otp import verify_otp as _core_verify_otp  # type: ignore
        return _core_verify_otp(user, code, request=request)
    except Exception:
        pass

    from django.core.cache import cache

    expected = cache.get(f"poi-otp:{user.pk}")
    if expected and str(code).strip() == str(expected):
        cache.delete(f"poi-otp:{user.pk}")
        return True
    return False


# ─────────────────────────────────────────────────────────────────────────────
# 4. NOTIFICATION
# ─────────────────────────────────────────────────────────────────────────────
# ‼️ RECONCILE: set to your real notification model + its field names. We only
# READ notifications for the POI feed; creation stays in your existing code.
NOTIFICATION_MODEL = getattr(settings, "POI_NOTIFICATION_MODEL", "core.Notification")
NOTIF_RECIPIENT_FIELD = getattr(settings, "POI_NOTIF_RECIPIENT_FIELD", "user")
NOTIF_READ_FIELD = getattr(settings, "POI_NOTIF_READ_FIELD", "is_read")
NOTIF_CREATED_FIELD = getattr(settings, "POI_NOTIF_CREATED_FIELD", "created_at")


def notifications_for(user, unread_only=False, limit=50):
    """Return a queryset (or list) of notifications for *user*, newest first."""
    try:
        Model = apps.get_model(NOTIFICATION_MODEL)
    except Exception:
        log.exception("POI: could not resolve NOTIFICATION_MODEL=%r", NOTIFICATION_MODEL)
        return []
    try:
        qs = Model.objects.filter(**{NOTIF_RECIPIENT_FIELD: user})
        if unread_only:
            qs = qs.filter(**{NOTIF_READ_FIELD: False})
        return qs.order_by(f"-{NOTIF_CREATED_FIELD}")[:limit]
    except Exception:
        log.exception("POI: notifications_for failed")
        return []
