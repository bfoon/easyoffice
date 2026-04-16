"""
apps/files/collabora/tokens.py

Short-lived signed tokens that travel as WOPI `access_token` parameters
into the Collabora iframe.

We use Django's built-in `django.core.signing` rather than a JWT library
because:
  - No new dependency.
  - It's already HMAC-signed using SECRET_KEY (or COLLABORA_JWT_SECRET if set).
  - Has native TTL via max_age.
  - The output is URL-safe by default.

Token payload shape:
    {
        'uid':  <user_id>,
        'fid':  '<file_uuid>',
        'perm': 'view' | 'edit' | 'full',
        'sid':  '<session_uuid>',   # CollaboraSession id
    }

Tokens expire after COLLABORA_TOKEN_TTL_SECONDS (default 10 hours).
Collabora refreshes its own token internally during an editing session,
so this TTL only needs to be long enough for a typical editing stint
plus the final save-back.
"""
from django.core import signing
from django.conf import settings


_SALT = 'files.collabora.wopi.v1'


def _ttl():
    return getattr(settings, 'COLLABORA_TOKEN_TTL_SECONDS', 60 * 60 * 10)


def _secret():
    """
    Prefer a dedicated secret so rotating it doesn't invalidate user
    sessions across the rest of the site. Falls back to SECRET_KEY.
    """
    return getattr(settings, 'COLLABORA_JWT_SECRET', None) or settings.SECRET_KEY


def issue_token(user, file, permission, session_id):
    """
    Build a signed token string that authorizes `user` to access `file`
    with `permission` for the configured TTL.

    Args:
        user: Django User instance.
        file: SharedFile instance.
        permission: one of 'view', 'edit', 'full'.
        session_id: the CollaboraSession uuid (str or UUID).

    Returns:
        A URL-safe string suitable for use as WOPI `access_token`.
    """
    payload = {
        'uid':  user.pk,
        'fid':  str(file.pk),
        'perm': permission,
        'sid':  str(session_id),
    }
    signer = signing.TimestampSigner(key=_secret(), salt=_SALT)
    # Dump to a compact dict-signed blob, then wrap with timestamp.
    raw = signing.dumps(payload, key=_secret(), salt=_SALT, compress=True)
    return signer.sign(raw)


def verify_token(token):
    """
    Parse and verify a token. Raises `signing.BadSignature` or
    `signing.SignatureExpired` on failure.

    Returns the decoded payload dict:
        {'uid', 'fid', 'perm', 'sid'}
    """
    if not token:
        raise signing.BadSignature('empty token')
    signer = signing.TimestampSigner(key=_secret(), salt=_SALT)
    raw = signer.unsign(token, max_age=_ttl())
    return signing.loads(raw, key=_secret(), salt=_SALT)
