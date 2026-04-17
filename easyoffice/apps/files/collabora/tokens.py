"""
apps/files/collabora/tokens.py

Short-lived signed tokens that travel as WOPI `access_token` parameters
into the Collabora iframe.
"""
from django.core import signing
from django.conf import settings


_SALT = 'files.collabora.wopi.v1'


def _ttl():
    return getattr(settings, 'COLLABORA_TOKEN_TTL_SECONDS', 60 * 60 * 10)


def _secret():
    return getattr(settings, 'COLLABORA_JWT_SECRET', None) or settings.SECRET_KEY


def issue_token(user, file, permission, session_id):
    """
    Build a signed token string. Signs only once using TimestampSigner
    so the token is a clean, URL-safe string with no leading dot.
    """
    payload = {
        'uid':  str(user.pk),
        'fid':  str(file.pk),
        'perm': permission,
        'sid':  str(session_id),
    }
    # Single sign — dumps() already produces a signed, timestamped blob.
    return signing.dumps(payload, key=_secret(), salt=_SALT, compress=True)


def verify_token(token):
    """
    Parse and verify a token. Raises signing.BadSignature or
    signing.SignatureExpired on failure.

    Returns the decoded payload dict: {'uid', 'fid', 'perm', 'sid'}
    """
    if not token:
        raise signing.BadSignature('empty token')
    return signing.loads(
        token,
        key=_secret(),
        salt=_SALT,
        max_age=_ttl(),
    )