"""
apps/inventory/api/models.py
────────────────────────────
Hashed API keys for the inventory public API.

We never store the raw key. The `issue()` classmethod returns a tuple
(api_key_record, raw_key_string) — the caller is responsible for showing
the raw key to the user once and never persisting it elsewhere.
"""
from __future__ import annotations

import hashlib
import secrets
import uuid

from django.conf import settings
from django.db import models


User = settings.AUTH_USER_MODEL


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


class ApiKey(models.Model):
    """A long-lived API key bound to a user, used for the inventory API."""
    id            = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user          = models.ForeignKey(User, on_delete=models.CASCADE, related_name='inventory_api_keys')
    name          = models.CharField(max_length=120, help_text='Human-readable label (e.g. "Warehouse scanner #1")')

    # Storage
    key_hash      = models.CharField(max_length=64, unique=True, editable=False)
    lookup_prefix = models.CharField(max_length=12, unique=True, editable=False, db_index=True)

    # Lifecycle
    is_active     = models.BooleanField(default=True)
    expires_at    = models.DateTimeField(null=True, blank=True)
    last_used_at  = models.DateTimeField(null=True, blank=True)

    # Audit
    created_by    = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='+',
    )
    created_at    = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Inventory API key'

    def __str__(self):
        return f'{self.name} — {self.lookup_prefix}…'

    # ── Issue ──────────────────────────────────────────────────────────────
    @classmethod
    def issue(cls, *, user, name: str, created_by=None, expires_at=None):
        """
        Generate a new API key. Returns (record, raw_key_string).

        The raw key is shown to the caller exactly once. Subsequent
        retrieval is impossible — only re-issuance.
        """
        raw = 'sk_inv_' + secrets.token_urlsafe(32)
        record = cls.objects.create(
            user=user,
            name=name,
            key_hash=_hash(raw),
            lookup_prefix=raw[:12],
            created_by=created_by,
            expires_at=expires_at,
        )
        return record, raw
