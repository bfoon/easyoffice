"""
apps/customer_portal/device_binding.py
=======================================

Device-binding helper used by the token gate.

Why this exists
---------------
A portal token in a URL is convenient but easy to leak (forwarded email,
browser sync across machines, screenshare). Binding usage to a specific
machine gives us a second factor without making the customer log in.

How a "device" is identified
----------------------------
We compute a SHA-256 of three things:

  1. A long random `device_seed` we plant in the customer's browser
     (signed cookie + a localStorage mirror, see template). The cookie is
     scoped to the portal path, signed with SECRET_KEY, and HttpOnly so
     it survives across sessions but isn't readable by JS.
  2. A trimmed User-Agent string. Stable across visits from the same
     browser; differs across browsers.
  3. The Accept-Language header. Mostly stable per-user.

This is NOT canvas/WebGL fingerprinting. It's not bulletproof — but for
"is this the same browser the customer used last time?" it's plenty, and
it doesn't track the customer in any privacy-invasive way.

Resolution flow
---------------
    request → resolve_device_state(token, request)
         ├── known + verified  → DeviceState(verified=True, binding=...)
         ├── unknown / new     → DeviceState(verified=False, fingerprint=...)
         └── revoked           → DeviceState(verified=False, blocked=True)

The view then either lets the request through (verified) or hands it
off to VerifyDeviceView (unknown).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from django.conf import settings
from django.core.signing import BadSignature, TimestampSigner
from django.utils import timezone

from .models import (
    DeviceBinding, DeviceOTP, PortalAccessToken,
    _hash_fingerprint, _make_device_seed,
)

logger = logging.getLogger(__name__)


COOKIE_NAME = 'portal_did'
COOKIE_MAX_AGE = 60 * 60 * 24 * 365  # 1 year — token expiry is the real ceiling
SIGNER_SALT = 'customer_portal.device_binding'


# ─────────────────────────────────────────────────────────────────────────────
# Cookie plumbing
# ─────────────────────────────────────────────────────────────────────────────

def _signer() -> TimestampSigner:
    return TimestampSigner(salt=SIGNER_SALT)


def get_or_assign_device_seed(request, response=None) -> tuple[str, bool]:
    """
    Read the signed device-seed cookie, or mint a new one. Returns
    (seed, was_new). If `response` is given AND the seed is new, attach
    the cookie. The view layer is responsible for actually returning the
    response.
    """
    raw = request.COOKIES.get(COOKIE_NAME)
    if raw:
        try:
            seed = _signer().unsign(raw, max_age=COOKIE_MAX_AGE)
            return seed, False
        except BadSignature:
            # Tampered or expired — treat as new.
            pass

    seed = _make_device_seed()
    if response is not None:
        signed = _signer().sign(seed)
        response.set_cookie(
            COOKIE_NAME, signed,
            max_age=COOKIE_MAX_AGE,
            secure=not settings.DEBUG,
            httponly=True,
            samesite='Lax',
        )
    return seed, True


def attach_device_cookie(response, seed: str) -> None:
    """Force-set the cookie (used after OTP verification)."""
    signed = _signer().sign(seed)
    response.set_cookie(
        COOKIE_NAME, signed,
        max_age=COOKIE_MAX_AGE,
        secure=not settings.DEBUG,
        httponly=True,
        samesite='Lax',
    )


# ─────────────────────────────────────────────────────────────────────────────
# Fingerprinting
# ─────────────────────────────────────────────────────────────────────────────

def _trimmed_ua(request) -> str:
    return (request.META.get('HTTP_USER_AGENT') or '')[:400]


def _accept_lang(request) -> str:
    return (request.META.get('HTTP_ACCEPT_LANGUAGE') or '')[:80]


def compute_fingerprint(request, seed: str) -> str:
    """
    The hash that uniquely identifies this browser-on-this-machine for a
    given seed. Stable across pageloads from the same browser.
    """
    return _hash_fingerprint(seed, _trimmed_ua(request), _accept_lang(request))


def device_label(request) -> str:
    """Human-readable hint for the admin: 'Chrome 124 · macOS · Banjul'."""
    ua = _trimmed_ua(request)
    parts = []
    # Crude UA parsing — good enough for "what device is this"
    if 'Edg/' in ua: parts.append('Edge')
    elif 'Chrome/' in ua: parts.append('Chrome')
    elif 'Firefox/' in ua: parts.append('Firefox')
    elif 'Safari/' in ua and 'Chrome/' not in ua: parts.append('Safari')

    if 'Windows' in ua: parts.append('Windows')
    elif 'Mac OS X' in ua or 'Macintosh' in ua: parts.append('macOS')
    elif 'Android' in ua: parts.append('Android')
    elif 'iPhone' in ua or 'iPad' in ua: parts.append('iOS')
    elif 'Linux' in ua: parts.append('Linux')

    return ' · '.join(parts) if parts else 'Unknown device'


def _client_ip(request) -> Optional[str]:
    xf = request.META.get('HTTP_X_FORWARDED_FOR', '')
    if xf:
        return xf.split(',')[0].strip() or None
    return request.META.get('REMOTE_ADDR') or None


# ─────────────────────────────────────────────────────────────────────────────
# Resolution
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class DeviceState:
    seed: str
    seed_was_new: bool
    fingerprint: str
    binding: Optional[DeviceBinding]
    is_verified: bool
    is_blocked: bool  # active revocation on a matching binding


def resolve_device_state(token: PortalAccessToken, request) -> DeviceState:
    """
    Don't write to the response in here — the caller decides whether to
    set the cookie (e.g. only on a successful HTML response).
    """
    seed, was_new = get_or_assign_device_seed(request, response=None)
    fp = compute_fingerprint(request, seed)

    binding = (
        DeviceBinding.objects
        .filter(token=token, fingerprint_hash=fp)
        .order_by('-created_at')
        .first()
    )

    is_verified = bool(binding and binding.is_active)
    is_blocked = bool(binding and not binding.is_active)

    if is_verified:
        try:
            binding.touch(ip=_client_ip(request))
        except Exception:
            logger.exception('customer_portal: device binding touch failed')

    return DeviceState(
        seed=seed,
        seed_was_new=was_new,
        fingerprint=fp,
        binding=binding,
        is_verified=is_verified,
        is_blocked=is_blocked,
    )


# ─────────────────────────────────────────────────────────────────────────────
# OTP issuance & verification
# ─────────────────────────────────────────────────────────────────────────────

def issue_device_otp(
    *,
    token: PortalAccessToken,
    fingerprint: str,
    request,
    purpose: str = 'device_verify',
) -> tuple[DeviceOTP, str]:
    """
    Mint a fresh OTP, invalidate any prior un-consumed OTPs for the same
    (token, fingerprint), persist the hash, and return (otp_row, plain_code).
    The caller emails plain_code; we never persist it.
    """
    from .models import _make_otp_code

    # Invalidate stale codes for this device
    (DeviceOTP.objects
        .filter(
            token=token,
            fingerprint_hash=fingerprint,
            consumed_at__isnull=True,
            invalidated_at__isnull=True,
        )
        .update(invalidated_at=timezone.now()))

    code = _make_otp_code()
    otp = DeviceOTP.objects.create(
        token=token,
        fingerprint_hash=fingerprint,
        code_hash=DeviceOTP.hash_code(code),
        purpose=purpose,
        expires_at=timezone.now() + DeviceOTP.OTP_TTL,
        delivered_to_email=token.contact.email,
        requested_ip=_client_ip(request),
        requested_user_agent=_trimmed_ua(request),
    )
    return otp, code


def verify_device_otp(
    *,
    token: PortalAccessToken,
    fingerprint: str,
    code: str,
    request,
) -> tuple[bool, str, Optional[DeviceBinding]]:
    """
    Returns (ok, message, binding_or_none). On success, creates (or
    re-activates) the DeviceBinding and consumes the OTP.
    """
    code = (code or '').strip()
    if not code or not code.isdigit() or len(code) != 6:
        return False, 'Please enter the 6-digit code from your email.', None

    otp = (
        DeviceOTP.objects
        .filter(
            token=token,
            fingerprint_hash=fingerprint,
            consumed_at__isnull=True,
            invalidated_at__isnull=True,
        )
        .order_by('-issued_at')
        .first()
    )
    if not otp:
        return False, 'No active verification code — please request a new one.', None

    if not otp.is_consumable:
        if timezone.now() > otp.expires_at:
            return False, 'That code has expired. Please request a new one.', None
        return False, 'Too many attempts on this code. Please request a new one.', None

    otp.mark_attempt()
    if DeviceOTP.hash_code(code) != otp.code_hash:
        remaining = max(0, DeviceOTP.MAX_ATTEMPTS - otp.attempts)
        if remaining == 0:
            otp.invalidate(reason='max attempts')
            return False, 'Too many attempts. Please request a new code.', None
        return False, f"That code didn't match. {remaining} attempt{'s' if remaining != 1 else ''} left.", None

    # Success — consume + bind.
    otp.consume()

    binding, created = DeviceBinding.objects.get_or_create(
        token=token,
        fingerprint_hash=fingerprint,
        defaults={
            'label': device_label(request),
            'user_agent': _trimmed_ua(request),
            'ip_first_seen': _client_ip(request),
            'ip_last_seen': _client_ip(request),
        },
    )
    if not created and binding.revoked_at:
        # Re-activate a revoked binding when the customer re-verifies.
        binding.revoked_at = None
        binding.revoked_reason = ''
        binding.verified_at = timezone.now()
        binding.last_seen_at = timezone.now()
        binding.use_count = 1
        binding.save(update_fields=[
            'revoked_at', 'revoked_reason', 'verified_at',
            'last_seen_at', 'use_count', 'updated_at',
        ])
    elif not created:
        binding.last_seen_at = timezone.now()
        binding.ip_last_seen = _client_ip(request)
        binding.save(update_fields=['last_seen_at', 'ip_last_seen', 'updated_at'])

    return True, 'Device verified.', binding
