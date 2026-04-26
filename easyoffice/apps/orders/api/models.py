"""
apps/orders/api/models.py
─────────────────────────
ApiKey records for the public orders endpoint. Stored hashed; the raw key
is shown to the issuer ONCE at creation time and never again.
"""
import hashlib
import secrets
from django.conf import settings
from django.db import models


class ApiKey(models.Model):
    """
    One ApiKey row per integration partner (website, mobile app, etc.).

    Each key is bound to a User account — that's the user the API request
    runs as for permission purposes. We recommend creating a dedicated
    `website-bot` (or similar) user, putting it in the right groups, and
    issuing keys against it.
    """
    name        = models.CharField(max_length=100,
                                    help_text='Human label, e.g. "Website production"')
    key_hash    = models.CharField(max_length=64, unique=True, editable=False)
    # First few chars of the raw key, kept in cleartext purely for display
    # in admin / settings ("Api-Key sk_live_abc1...") so people can match
    # rows to known keys without revealing the full secret.
    key_prefix  = models.CharField(max_length=8, blank=True, editable=False)
    user        = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name='order_api_keys',
        help_text='The user identity this key authenticates as.',
    )
    is_active   = models.BooleanField(default=True)
    created_at  = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.name} ({self.key_prefix}…)'

    # ── Issuance helper ─────────────────────────────────────────────────────
    @classmethod
    def issue(cls, *, name, user):
        """
        Create a new key. Returns (api_key_instance, raw_key_string).
        The raw key is the only time the caller will see it — show it
        once, then forget it.
        """
        raw = 'sk_' + secrets.token_urlsafe(32)
        digest = hashlib.sha256(raw.encode()).hexdigest()
        instance = cls.objects.create(
            name=name[:100],
            key_hash=digest,
            key_prefix=raw[:8],
            user=user,
        )
        return instance, raw
