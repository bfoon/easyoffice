"""
apps/inventory/api/authentication.py
────────────────────────────────────
Custom DRF authentication using `Authorization: Api-Key <raw>` header.

The raw key is *never* stored — only its SHA-256 hash. We match by lookup
prefix (the first 12 chars of the raw key, stored alongside the hash) so
authentication is O(1) instead of O(n).

This module mirrors the shape used by apps.orders.api.authentication.
"""
from __future__ import annotations

import hashlib

from django.utils import timezone
from rest_framework import authentication, exceptions

from .models import ApiKey


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


class ApiKeyAuthentication(authentication.BaseAuthentication):
    """
    Accepts either:
        Authorization: Api-Key sk_xxxx
        X-Api-Key: sk_xxxx
    """
    keyword = 'api-key'

    def authenticate(self, request):
        raw = self._extract_key(request)
        if not raw:
            return None  # let other auth backends try

        prefix = raw[:12]
        try:
            api_key = ApiKey.objects.select_related('user').get(
                lookup_prefix=prefix, is_active=True,
            )
        except ApiKey.DoesNotExist:
            raise exceptions.AuthenticationFailed('Invalid API key.')

        if api_key.key_hash != _hash(raw):
            raise exceptions.AuthenticationFailed('Invalid API key.')

        if api_key.expires_at and api_key.expires_at < timezone.now():
            raise exceptions.AuthenticationFailed('API key has expired.')

        # Record usage (best-effort — don't fail the request on this)
        try:
            ApiKey.objects.filter(pk=api_key.pk).update(last_used_at=timezone.now())
        except Exception:
            pass

        if not api_key.user or not api_key.user.is_active:
            raise exceptions.AuthenticationFailed('API key user is inactive.')

        return (api_key.user, api_key)

    def authenticate_header(self, request):
        return 'Api-Key'

    def _extract_key(self, request):
        # X-Api-Key header
        x_key = request.META.get('HTTP_X_API_KEY')
        if x_key:
            return x_key.strip()

        # Authorization: Api-Key <raw>
        auth = request.META.get('HTTP_AUTHORIZATION', '')
        if not auth:
            return None
        parts = auth.split(None, 1)
        if len(parts) != 2:
            return None
        scheme, value = parts
        if scheme.lower() != self.keyword:
            return None
        return value.strip()
