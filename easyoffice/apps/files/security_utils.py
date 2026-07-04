"""
apps/files/security_utils.py
────────────────────────────
Shared security helpers for the files app. Fixes several issues found in
the audit:

  1. Content-Disposition header injection — filenames containing quotes,
     CR/LF or non-ASCII were interpolated raw into headers.
  2. Stored-XSS via inline serving — user-uploaded HTML/SVG/XHTML was
     served inline on the app origin with a guessed content type.
  3. X-Forwarded-For spoofing — the first XFF hop was trusted blindly,
     which poisons audit logs and device fingerprints.
  4. No rate limiting on anonymous token endpoints — the external-share
     fingerprint endpoint could be used to email-bomb a share owner.
  5. Display-name hygiene — file renames accepted control characters,
     path separators, and literal "\\u002D" artefacts.

Usage is intentionally drop-in:

    from apps.files.security_utils import (
        set_content_disposition, secure_file_response,
        client_ip, rate_limit, clean_display_name,
    )
"""
from __future__ import annotations

import mimetypes
import re
import time
import unicodedata
from urllib.parse import quote

from django.conf import settings
from django.core.cache import cache
from django.http import FileResponse


# ─────────────────────────────────────────────────────────────────────────────
# 1) Safe Content-Disposition (RFC 6266 / RFC 5987)
# ─────────────────────────────────────────────────────────────────────────────

def _ascii_fallback(filename: str) -> str:
    """
    ASCII-only fallback for legacy clients: strip CR/LF/quotes/backslashes
    and transliterate what we can.
    """
    normalized = unicodedata.normalize('NFKD', filename)
    ascii_name = normalized.encode('ascii', 'ignore').decode('ascii')
    ascii_name = re.sub(r'[\r\n"\\\x00-\x1f\x7f]', '', ascii_name).strip()
    return ascii_name or 'download'


def content_disposition(filename: str, inline: bool = False) -> str:
    """
    Build a header value that is immune to header injection and preserves
    Unicode names (spaces included) via the filename* parameter.

        Content-Disposition: attachment; filename="My Report.pdf";
                             filename*=UTF-8''My%20Report.pdf
    """
    disposition = 'inline' if inline else 'attachment'
    filename = (filename or 'download').strip()
    fallback = _ascii_fallback(filename)
    utf8_quoted = quote(filename, safe='')
    return f'{disposition}; filename="{fallback}"; filename*=UTF-8\'\'{utf8_quoted}'


def set_content_disposition(response, filename: str, inline: bool = False):
    response['Content-Disposition'] = content_disposition(filename, inline=inline)
    return response


# ─────────────────────────────────────────────────────────────────────────────
# 2) Safe inline serving — never let user uploads execute on our origin
# ─────────────────────────────────────────────────────────────────────────────

# Types that are safe to render inline in a browser tab / iframe.
_INLINE_SAFE_TYPES = {
    'application/pdf',
    'image/png', 'image/jpeg', 'image/gif', 'image/webp', 'image/bmp',
    'audio/mpeg', 'audio/ogg', 'audio/wav', 'audio/mp4',
    'video/mp4', 'video/webm', 'video/ogg',
    'text/plain',
}

# Types that can execute script or navigate when rendered inline.
_ACTIVE_CONTENT_TYPES = {
    'text/html', 'application/xhtml+xml', 'image/svg+xml',
    'text/xml', 'application/xml', 'text/javascript',
    'application/javascript', 'application/x-httpd-php',
}


def secure_file_response(fh, filename: str, *, inline: bool = False,
                         content_type: str | None = None) -> FileResponse:
    """
    Wrap FileResponse with the right headers:

      • guessed/declared content type is downgraded to a download or to
        text/plain if it could execute script on our origin;
      • X-Content-Type-Options: nosniff always set;
      • CSP sandbox on anything served inline, so even a mis-typed file
        cannot run script or reach our cookies;
      • injection-proof Content-Disposition.
    """
    if content_type is None:
        content_type, _ = mimetypes.guess_type(filename or '')
    content_type = (content_type or 'application/octet-stream').split(';')[0].strip().lower()

    if content_type in _ACTIVE_CONTENT_TYPES:
        if inline:
            # Show the source instead of executing it.
            content_type = 'text/plain; charset=utf-8'
        else:
            content_type = 'application/octet-stream'
    elif inline and content_type not in _INLINE_SAFE_TYPES:
        # Unknown types are downloaded, not rendered.
        inline = False
        content_type = 'application/octet-stream'

    response = FileResponse(fh, content_type=content_type)
    set_content_disposition(response, filename, inline=inline)
    response['X-Content-Type-Options'] = 'nosniff'
    if inline:
        # Belt and braces: even if a browser sniffs, no script/plugins/forms.
        response['Content-Security-Policy'] = (
            "sandbox; default-src 'none'; img-src data: blob:; "
            "media-src data: blob:; style-src 'unsafe-inline'"
        )
        response['X-Frame-Options'] = 'SAMEORIGIN'
    return response


# ─────────────────────────────────────────────────────────────────────────────
# 3) Client IP that respects a trusted proxy count
# ─────────────────────────────────────────────────────────────────────────────

def client_ip(request) -> str:
    """
    X-Forwarded-For is attacker-controlled unless you only trust the hops
    added by your own proxies. Configure in settings:

        TRUSTED_PROXY_COUNT = 1   # e.g. one nginx in front of gunicorn

    With N trusted proxies the real client is the Nth address from the
    right of the XFF chain. With 0 we use REMOTE_ADDR only.
    """
    trusted = int(getattr(settings, 'TRUSTED_PROXY_COUNT', 0) or 0)
    if trusted > 0:
        fwd = request.META.get('HTTP_X_FORWARDED_FOR', '')
        if fwd:
            hops = [h.strip() for h in fwd.split(',') if h.strip()]
            if len(hops) >= trusted:
                return hops[-trusted]
    return request.META.get('REMOTE_ADDR') or ''


# ─────────────────────────────────────────────────────────────────────────────
# 4) Tiny cache-backed rate limiter for anonymous token endpoints
# ─────────────────────────────────────────────────────────────────────────────

def rate_limit(key: str, *, limit: int, window_seconds: int) -> bool:
    """
    Returns True if the caller is within the limit, False if they should
    be rejected. Fixed-window counter on the default cache — good enough
    for abuse suppression on public endpoints.

        if not rate_limit(f'extfp:{ip}', limit=20, window_seconds=3600):
            return JsonResponse({'ok': False, 'error': 'rate_limited'}, status=429)
    """
    window = int(time.time() // window_seconds)
    cache_key = f'rl:{key}:{window}'
    try:
        current = cache.get_or_set(cache_key, 0, timeout=window_seconds + 5)
        if current >= limit:
            return False
        try:
            cache.incr(cache_key)
        except ValueError:
            cache.set(cache_key, 1, timeout=window_seconds + 5)
        return True
    except Exception:
        # A broken cache must never take the endpoint down.
        return True


# ─────────────────────────────────────────────────────────────────────────────
# 5) Display-name hygiene (also repairs the \u002D artefacts)
# ─────────────────────────────────────────────────────────────────────────────

# Matches literal backslash-u escape artefacts that leaked into stored
# names from the old escapejs-in-data-attribute bug, e.g. "\u002D".
_JS_ESCAPE_ARTEFACT = re.compile(r'\\u([0-9a-fA-F]{4})')

_FORBIDDEN_NAME_CHARS = re.compile(r'[\x00-\x1f\x7f/\\:*?"<>|]')


def clean_display_name(name: str, max_length: int = 255) -> str:
    """
    Normalise a user-supplied file/folder display name:

      • decode literal "\\uXXXX" artefacts back to their characters
        (this repairs names corrupted by the escapejs template bug);
      • strip control characters, path separators, and shell-hostile
        punctuation;
      • collapse runs of whitespace;
      • trim leading/trailing dots and spaces (Windows compatibility).
    """
    name = str(name or '')

    def _decode(match):
        try:
            return chr(int(match.group(1), 16))
        except Exception:
            return ''
    name = _JS_ESCAPE_ARTEFACT.sub(_decode, name)

    name = _FORBIDDEN_NAME_CHARS.sub('', name)
    name = re.sub(r'\s+', ' ', name).strip().strip('.')
    return name[:max_length] or 'Untitled'
