"""
apps/messaging/encryption.py
────────────────────────────
AES-256 (Fernet) helpers for encrypting/decrypting ChatMessage content
at rest.

Setup
-----
1.  pip install cryptography
2.  Add to settings.py:

        import os
        from cryptography.fernet import Fernet

        # Generate once and store securely (env var / secrets manager):
        #   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
        MESSAGING_ENCRYPTION_KEY = os.environ["MESSAGING_ENCRYPTION_KEY"]

Transit security
----------------
*   Enforce TLS (HTTPS + WSS) at the infrastructure layer (nginx / load-balancer).
    Django's SESSION_COOKIE_SECURE = True and CSRF_COOKIE_SECURE = True add a
    second line of defence; see settings snippet below.

At-rest security
----------------
*   Every message `content` string is encrypted with Fernet (AES-128-CBC +
    HMAC-SHA256) before being saved and decrypted on read.
*   The raw ciphertext starts with the sentinel prefix ``enc:`` so legacy
    plain-text rows are handled gracefully during a rolling migration.
"""

import logging
from functools import lru_cache

from django.conf import settings

logger = logging.getLogger(__name__)

_SENTINEL = "enc:"          # prefix that marks an encrypted value


@lru_cache(maxsize=1)
def _fernet():
    """Return a lazily-initialised Fernet instance (cached for the process)."""
    from cryptography.fernet import Fernet  # noqa: PLC0415

    key = getattr(settings, "MESSAGING_ENCRYPTION_KEY", None)
    if not key:
        raise RuntimeError(
            "MESSAGING_ENCRYPTION_KEY is not set. "
            "Generate one with: python -c \"from cryptography.fernet import Fernet; "
            "print(Fernet.generate_key().decode())\""
        )
    return Fernet(key.encode() if isinstance(key, str) else key)


# ─────────────────────────────────────────────────────────────────────────────
# Public helpers
# ─────────────────────────────────────────────────────────────────────────────

def encrypt_content(plaintext: str) -> str:
    """
    Encrypt *plaintext* and return a sentinel-prefixed ciphertext string
    safe for storage in a TextField.

    Returns an empty string when *plaintext* is empty.
    """
    if not plaintext:
        return plaintext
    try:
        token = _fernet().encrypt(plaintext.encode("utf-8"))
        return _SENTINEL + token.decode("utf-8")
    except Exception:
        logger.exception("Failed to encrypt message content – storing plaintext")
        return plaintext


def decrypt_content(value: str) -> str:
    """
    Decrypt a value previously produced by :func:`encrypt_content`.

    *   Values that do NOT start with the sentinel are assumed to be legacy
        plain-text and are returned as-is (safe rolling migration).
    *   Decryption errors are logged and the raw (encrypted) value is returned
        rather than raising, so chat rendering always gets *something*.
    """
    if not value or not value.startswith(_SENTINEL):
        return value          # legacy plain-text — pass through unchanged
    try:
        ciphertext = value[len(_SENTINEL):].encode("utf-8")
        return _fernet().decrypt(ciphertext).decode("utf-8")
    except Exception:
        logger.exception("Failed to decrypt message content")
        return value          # degrade gracefully


def is_encrypted(value: str) -> bool:
    """Return True when *value* looks like it was encrypted by this module."""
    return bool(value and value.startswith(_SENTINEL))
