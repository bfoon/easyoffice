"""
apps/orders/api/authentication.py
─────────────────────────────────
Custom DRF auth: an API-key header that maps to a service User.

    Header: Authorization: Api-Key <key>

This sits alongside DRF's built-in TokenAuthentication and SimpleJWT's
JWTAuthentication. The view sets up all three so integrators can pick
whichever is easiest for them.

Storage: the API key model lives in this same package. Keys are stored
hashed (SHA-256) so a leaked DB row does not leak usable credentials.
"""
import hashlib
from rest_framework import authentication, exceptions

from .models import ApiKey


class ApiKeyAuthentication(authentication.BaseAuthentication):
    keyword = 'Api-Key'

    def authenticate(self, request):
        auth = authentication.get_authorization_header(request).split()
        if not auth or auth[0].lower() != self.keyword.lower().encode():
            return None
        if len(auth) == 1:
            raise exceptions.AuthenticationFailed('Invalid API key header. No key provided.')
        if len(auth) > 2:
            raise exceptions.AuthenticationFailed('Invalid API key header. Token may not contain spaces.')

        try:
            key = auth[1].decode()
        except UnicodeError:
            raise exceptions.AuthenticationFailed('Invalid API key header. Key contains invalid characters.')

        # Hash the presented key and look it up. Constant-time comparison
        # is implicit because we're matching a fixed-length hex digest.
        digest = hashlib.sha256(key.encode()).hexdigest()
        try:
            api_key = (
                ApiKey.objects.select_related('user')
                .get(key_hash=digest, is_active=True)
            )
        except ApiKey.DoesNotExist:
            raise exceptions.AuthenticationFailed('Invalid or revoked API key.')

        if api_key.user is None or not api_key.user.is_active:
            raise exceptions.AuthenticationFailed('API key is not bound to an active user.')

        # Touch last_used (best-effort, no error if it fails)
        try:
            from django.utils import timezone
            ApiKey.objects.filter(pk=api_key.pk).update(last_used_at=timezone.now())
        except Exception:
            pass

        return (api_key.user, api_key)

    def authenticate_header(self, request):
        return self.keyword
