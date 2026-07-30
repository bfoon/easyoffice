"""
settings_security.py
────────────────────
Transport security for EasyOffice messaging. Import it at the END of your
settings.py, after DEBUG and ALLOWED_HOSTS are decided::

    from .settings_security import *   # noqa: F401,F403

Then confirm it took effect::

    python manage.py check --deploy

⚠️  BECAUSE THIS IS A WILDCARD IMPORT AT THE END OF settings.py, EVERY NAME
    SET HERE SILENTLY OVERWRITES THE SAME NAME ABOVE IT — including values
    read from .env via config(). If a setting "won't take" from .env, look
    here first. Anything meant to be tunable per-environment must therefore
    read the environment *in this file*, not in settings.py.

WHAT THIS COVERS AND WHAT IT DOESN'T
------------------------------------
``apps/messaging/encryption.py`` encrypts message bodies AT REST, in the
database. This file covers the OTHER half: the wire.

Between them you get:

    browser  ──TLS──▶  nginx  ──▶  Django/Daphne  ──▶  Postgres
             (this file)                              (encryption.py)

What none of it gives you is end-to-end encryption. There is one
symmetric key on the server, so anyone holding that key and a database
dump reads everything. That is a deliberate trade — it keeps server-side
search, moderation and compliance possible. Say so plainly if anyone
asks, rather than letting "encrypted" imply more than it does.

The single most common way this protection is lost is a preview of the
message body in a push notification or an email: the text then travels
through Firebase or your mail relay in the clear and sits in their logs.
MESSAGING_NOTIFY_INCLUDE_PREVIEW below is what closes that.
"""

import os


def _env_bool(name, default=False):
    """
    Read a boolean from the environment. Accepts the spellings people
    actually put in .env files. Defined here rather than imported so this
    module stays standalone.
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ('1', 'true', 'yes', 'on')


# ─────────────────────────────────────────────────────────────────────────────
# 1. HTTPS for every request
# ─────────────────────────────────────────────────────────────────────────────
# nginx terminates TLS and forwards plain HTTP to Daphne, so Django sees an
# insecure request unless it is told to read the proxy's header. Without
# this line SECURE_SSL_REDIRECT causes an infinite redirect loop.
#
# The proxy MUST always overwrite X-Forwarded-Proto and never pass through a
# client-supplied one, or a caller can simply claim to be on HTTPS.
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')

SECURE_SSL_REDIRECT = True

# Start at an hour, verify every subdomain serves HTTPS, then raise to a
# year. Preload only once you are certain — it is hard to undo.
#
# NOTE: nginx also sends Strict-Transport-Security (max-age=31536000).
# Browsers honour the FIRST header, so whichever arrives first wins and the
# other is dead weight. Pick one owner — either raise this to 31536000 and
# drop it from nginx, or drop these three lines and let nginx own it.
SECURE_HSTS_SECONDS = 3600
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = False

SECURE_REFERRER_POLICY = 'same-origin'
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = 'DENY'

# ─────────────────────────────────────────────────────────────────────────────
# 2. Cookies
# ─────────────────────────────────────────────────────────────────────────────
# The chat WebSocket authenticates with the session cookie in the browser,
# so these three settings protect the socket as much as the pages.
SESSION_COOKIE_SECURE = True
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = 'Lax'

CSRF_COOKIE_SECURE = True
CSRF_COOKIE_HTTPONLY = False        # the call window reads it to sign POSTs
CSRF_COOKIE_SAMESITE = 'Lax'

# Must be https:// and must list every hostname the app is served on.
CSRF_TRUSTED_ORIGINS = [
    o for o in os.environ.get('CSRF_TRUSTED_ORIGINS', '').split(',') if o.strip()
] or [
    'https://easyoffice.gm',
    'https://www.easyoffice.gm',
]

# ─────────────────────────────────────────────────────────────────────────────
# 3. WebSocket
# ─────────────────────────────────────────────────────────────────────────────
# Channels validates the socket's Origin header against ALLOWED_HOSTS, so a
# wildcard there lets any website on the internet open a chat socket using a
# signed-in visitor's cookies. Keep ALLOWED_HOSTS explicit.
#
# Tokens in query strings end up in nginx access logs, every proxy in
# between, and browser history. Native apps can set headers on a WebSocket;
# browsers use the session cookie.
#
# This USED to be a hard-coded False, which silently overrode the config()
# call in settings.py and left the Flutter app — which still connects with
# ?token=<jwt> — unable to authenticate at all, with no error beyond a bare
# 403 on the handshake. It now reads the environment so the rollout can be
# controlled per-deployment:
#
#     MESSAGING_WS_ALLOW_QUERY_TOKEN=True    # in .env, TEMPORARY
#
# Turn it back off the moment the mobile app ships a build that sends
# `Authorization: Bearer`. While it is on, every mobile access token is
# written in plaintext to the nginx and uvicorn access logs — treat those
# files as credential material and keep retention short.
MESSAGING_WS_ALLOW_QUERY_TOKEN = _env_bool('MESSAGING_WS_ALLOW_QUERY_TOKEN', False)

# Never turn this on outside a laptop: it permits authenticating a ws://
# socket, which carries every message in clear text.
MESSAGING_WS_ALLOW_INSECURE = _env_bool('MESSAGING_WS_ALLOW_INSECURE', False)

# ─────────────────────────────────────────────────────────────────────────────
# 4. Message encryption at rest
# ─────────────────────────────────────────────────────────────────────────────
# Generate once, store in the environment, never in source control:
#   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
MESSAGING_ENCRYPTION_KEY = os.environ.get('MESSAGING_ENCRYPTION_KEY', '')

# Fail closed. If encryption breaks, the save is refused rather than quietly
# writing readable rows. Only flip this during a deliberate, temporary
# operational emergency.
MESSAGING_ENCRYPTION_FAIL_OPEN = False

# Push and email notifications carry the sender's name only. Including the
# body would send it through Firebase and your mail relay in the clear,
# which defeats the encryption for anyone with access to those logs.
MESSAGING_NOTIFY_INCLUDE_PREVIEW = False

# ─────────────────────────────────────────────────────────────────────────────
# 5. Calls
# ─────────────────────────────────────────────────────────────────────────────
# Call media is always end-to-end encrypted by WebRTC itself (DTLS-SRTP),
# including when it passes through the relay — the relay forwards packets it
# cannot read. What the relay solves is reachability, not privacy.
#
# Run `python manage.py turncheck` after setting these.
WEBRTC_STUN_URLS = [
    'stun:stun.l.google.com:19302',
    'stun:turn.easyoffice.gm:3478',          # your own; don't depend on Google
]

WEBRTC_TURN_HOSTS = [
    'turn:turn.easyoffice.gm:3478?transport=udp',
    'turn:turn.easyoffice.gm:3478?transport=tcp',
    'turns:turn.easyoffice.gm:443?transport=tcp',   # the one that beats a proxy
]

# Must match `static-auth-secret` in turnserver.conf, byte for byte.
WEBRTC_TURN_SECRET = os.environ.get('WEBRTC_TURN_SECRET', '')
WEBRTC_TURN_REALM = os.environ.get('WEBRTC_TURN_REALM', 'easyoffice.gm')
WEBRTC_TURN_TTL = 3600

# Forces every call through the relay. Use it once to prove the relay works,
# then turn it off — it puts all media through your server.
WEBRTC_FORCE_RELAY = False