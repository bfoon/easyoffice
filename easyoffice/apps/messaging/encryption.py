"""
apps/messaging/encryption.py
────────────────────────────
Fernet helpers for encrypting/decrypting ChatMessage content at rest.

NOTE: Fernet = AES-128-CBC + HMAC-SHA256. (The previous docstring
claimed "AES-256", which was inaccurate.)

Setup
-----
1.  pip install cryptography
2.  Add to settings.py:

        import os

        # Generate once and store securely (env var / secrets manager):
        #   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
        MESSAGING_ENCRYPTION_KEY = os.environ["MESSAGING_ENCRYPTION_KEY"]

        # 🔒 Fail-closed by default: if encryption ever fails, raise
        # instead of silently storing plaintext. Only set this to True
        # temporarily during an emergency rollout.
        MESSAGING_ENCRYPTION_FAIL_OPEN = False

Transit security
----------------
*   Enforce TLS (HTTPS + WSS) at the infrastructure layer (nginx / LB).
    SESSION_COOKIE_SECURE = True and CSRF_COOKIE_SECURE = True add a
    second line of defence.

At-rest security
----------------
*   Every message `content` string is encrypted with Fernet before being
    saved and decrypted on read.
*   The ciphertext carries the sentinel prefix ``enc:`` so legacy
    plain-text rows are handled gracefully during a rolling migration.
*   ⚠️ This is server-side at-rest encryption with ONE symmetric key —
    anyone with the key + DB reads everything. It is NOT end-to-end
    encryption. Keep plaintext copies of content OUT of push
    notifications, emails, and logs or the encryption is pointless
    (see MESSAGING_NOTIFY_INCLUDE_PREVIEW in views.py).
"""

import logging
from functools import lru_cache

from django.conf import settings

logger = logging.getLogger(__name__)

_SENTINEL = "enc:"          # prefix that marks an encrypted value


class MessageEncryptionError(RuntimeError):
    """Raised when message content cannot be encrypted (fail-closed)."""


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

    🔒 SECURITY CHANGE: the old version caught every exception and stored
    PLAINTEXT with only a log line ("fail-open"). A single misconfigured
    worker (missing/typo'd key) would silently write unencrypted rows.
    We now fail CLOSED by default — the save raises and the message is
    rejected — unless MESSAGING_ENCRYPTION_FAIL_OPEN = True is set as a
    deliberate, temporary operational decision.
    """
    if not plaintext:
        return plaintext
    try:
        token = _fernet().encrypt(plaintext.encode("utf-8"))
        return _SENTINEL + token.decode("utf-8")
    except Exception as exc:
        logger.exception("Failed to encrypt message content")
        if getattr(settings, "MESSAGING_ENCRYPTION_FAIL_OPEN", False):
            # Explicit operator override: degrade to plaintext storage.
            return plaintext
        raise MessageEncryptionError(
            "Message encryption failed — refusing to store plaintext. "
            "Check MESSAGING_ENCRYPTION_KEY."
        ) from exc


def decrypt_content(value: str) -> str:
    """
    Decrypt a value previously produced by :func:`encrypt_content`.

    *   Values that do NOT start with the sentinel are assumed to be legacy
        plain-text and are returned as-is (safe rolling migration).
    *   Decryption errors are logged and the raw (encrypted) value is
        returned rather than raising, so chat rendering always gets
        *something*. (Reads staying graceful is fine — the ciphertext
        leaks nothing; only WRITES must fail closed.)
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
