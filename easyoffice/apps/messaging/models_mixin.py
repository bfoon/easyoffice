"""
apps/messaging/models_mixin.py
──────────────────────────────
Transparent at-rest encryption for ChatMessage.content (and any other model
that stores sensitive text).

Key design points
-----------------
1. save() encrypts content on its way to the DB but RESTORES the plaintext
   on the in-memory instance afterwards. This is critical: serializers,
   WebSocket broadcasts, and any code path that reads `instance.content`
   immediately after save() must get plaintext, not ciphertext.

2. post_init signal decrypts content every time the ORM loads a row, so
   reads work transparently everywhere.

3. Plain-text rows (no ``enc:`` prefix) are passed through untouched —
   enables rolling migration from plaintext to encrypted storage.
"""

from django.db import models
from django.db.models.signals import post_init
from django.dispatch import receiver

from apps.messaging.encryption import encrypt_content, decrypt_content, is_encrypted


class EncryptedContentMixin(models.Model):
    """
    Mixin that transparently encrypts ``content`` at rest.

    *   Plain-text rows written before encryption was enabled are decrypted
        gracefully (they have no ``enc:`` prefix so they pass through).
    *   Never double-encrypts: if ``content`` is already encrypted
        (starts with ``enc:``) ``save()`` skips re-encryption.
    *   Never leaves ciphertext on the in-memory instance after save()
        so serializers / WS broadcasts that read ``instance.content``
        post-save see plaintext.
    """

    class Meta:
        abstract = True

    # ── lifecycle ────────────────────────────────────────────────────────────

    def save(self, *args, **kwargs):
        # Remember the plaintext so we can restore it after the DB write.
        original_plaintext = self.content

        # Encrypt for the DB only if we actually have plaintext.
        if self.content and not is_encrypted(self.content):
            self.content = encrypt_content(self.content)

        try:
            super().save(*args, **kwargs)
        finally:
            # Restore plaintext on the in-memory instance so any code that
            # reads ``instance.content`` after save() (serializers,
            # WebSocket broadcasts, signals, etc.) sees the readable value.
            self.content = original_plaintext

    def decrypt(self):
        """
        Return the decrypted content without mutating the instance.
        Useful in serializers / templates when you need a one-off decryption.
        """
        return decrypt_content(self.content or "")


# ─────────────────────────────────────────────────────────────────────────────
# Auto-decrypt after ORM load
# ─────────────────────────────────────────────────────────────────────────────

def _connect_post_init(sender_class):
    """
    Register a post_init signal on *sender_class* so that every instance
    loaded from the database has its ``content`` field decrypted in place
    before any application code touches it.

    Call this once at the bottom of models.py:

        from apps.messaging.models_mixin import _connect_post_init
        _connect_post_init(ChatMessage)
    """

    @receiver(post_init, sender=sender_class, weak=False)
    def _decrypt_on_load(sender, instance, **kwargs):
        if instance.content and is_encrypted(instance.content):
            instance.content = decrypt_content(instance.content)