"""
apps/messaging/models_mixin.py
──────────────────────────────
Drop this mixin into ChatMessage (and any other model that stores sensitive
text) to get transparent at-rest encryption.

Usage in models.py
------------------

    from apps.messaging.models_mixin import EncryptedContentMixin

    class ChatMessage(EncryptedContentMixin, models.Model):
        content = models.TextField(blank=True, default='')
        # … rest of your fields …

How it works
------------
Django's ``save()`` is intercepted to encrypt ``content`` before the row
reaches the database.  A ``post_init`` signal transparently decrypts ``content``
right after the ORM loads the row, so the rest of your application always sees
plain text.

Key rotation
------------
Run the management command (see migrate_encrypt_messages.py) to re-encrypt
existing rows with a new key.  The ``enc:`` sentinel lets the command detect
plain-text rows that still need encrypting.
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
    *   The mixin never double-encrypts: if ``content`` is already encrypted
        (starts with ``enc:``) ``save()`` skips re-encryption.
    """

    class Meta:
        abstract = True

    # ── lifecycle ────────────────────────────────────────────────────────────

    def save(self, *args, **kwargs):
        # Encrypt content before writing to the database.
        if self.content and not is_encrypted(self.content):
            self.content = encrypt_content(self.content)
        super().save(*args, **kwargs)

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
