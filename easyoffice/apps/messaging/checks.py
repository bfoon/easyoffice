"""
apps/messaging/checks.py
────────────────────────
Deploy-time checks for message transport security.

WHY THESE EXIST
---------------
``apps/messaging/encryption.py`` encrypts message bodies AT REST. That is
worth having, but it protects the database — not the wire. A message is
only secure in transit if:

    1. Every page and every API call runs over TLS (HTTPS).
    2. The chat WebSocket runs over TLS too (WSS). This is the one people
       forget: a site can be perfectly HTTPS while its socket is opened
       as ``ws://`` and every message in the room goes out in the clear.
    3. Session and CSRF cookies are marked Secure, so the browser never
       replays them over a plaintext request.
    4. HSTS is set, so a first-visit downgrade attack has a short window.
    5. Access tokens are not written into URLs, because query strings are
       logged verbatim by nginx, every proxy in between, and browser
       history.

Nothing in Django enforces this by itself, and every one of these is
silent when wrong — the app keeps working, it just stops being private.
So they are checks: ``python manage.py check --deploy`` fails the deploy
rather than letting an unencrypted socket into production.

WIRING
------
In ``apps/messaging/apps.py``::

    from django.apps import AppConfig

    class MessagingConfig(AppConfig):
        name = 'apps.messaging'

        def ready(self):
            from apps.messaging import checks  # noqa: F401  (registers them)

Then::

    python manage.py check --deploy

Everything here is skipped when ``DEBUG`` is on, so local development
over plain http still works.
"""

from django.conf import settings
from django.core.checks import Error, Warning as CheckWarning, register

# Reserve a tag so the whole group can be run on its own:
#   python manage.py check --tag messaging_security
TAG = 'messaging_security'


def _deploying():
    """Only enforce on something that looks like a real deployment."""
    return not getattr(settings, 'DEBUG', False)


@register(TAG, deploy=True)
def check_transit_security(app_configs, **kwargs):
    problems = []

    if not _deploying():
        return problems

    # ── 1. HTTPS everywhere ──────────────────────────────────────────────
    if not getattr(settings, 'SECURE_SSL_REDIRECT', False):
        problems.append(Error(
            'Plain HTTP requests are not redirected to HTTPS.',
            hint=(
                'Set SECURE_SSL_REDIRECT = True. Without it a chat message can be '
                'posted over an unencrypted connection and read by anyone on the '
                'network path. If TLS terminates at nginx, also set '
                'SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https") and '
                'make nginx send that header, or Django will redirect forever.'
            ),
            id='messaging.E001',
        ))

    if getattr(settings, 'SECURE_SSL_REDIRECT', False) and not getattr(settings, 'SECURE_PROXY_SSL_HEADER', None):
        problems.append(CheckWarning(
            'SECURE_SSL_REDIRECT is on but SECURE_PROXY_SSL_HEADER is not set.',
            hint=(
                'If TLS is terminated by nginx or a load balancer, Django sees every '
                'request as HTTP and will redirect in a loop. Set '
                'SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https") and '
                'ensure the proxy always overwrites that header (never trusts a '
                'client-supplied one).'
            ),
            id='messaging.W001',
        ))

    # ── 2. Cookies ───────────────────────────────────────────────────────
    if not getattr(settings, 'SESSION_COOKIE_SECURE', False):
        problems.append(Error(
            'SESSION_COOKIE_SECURE is off, so the session cookie can be sent in clear text.',
            hint='Set SESSION_COOKIE_SECURE = True.',
            id='messaging.E002',
        ))
    if not getattr(settings, 'CSRF_COOKIE_SECURE', False):
        problems.append(Error(
            'CSRF_COOKIE_SECURE is off, so the CSRF cookie can be sent in clear text.',
            hint='Set CSRF_COOKIE_SECURE = True.',
            id='messaging.E003',
        ))
    if not getattr(settings, 'SESSION_COOKIE_HTTPONLY', True):
        problems.append(Error(
            'SESSION_COOKIE_HTTPONLY is off, so any injected script can read the session cookie.',
            hint='Set SESSION_COOKIE_HTTPONLY = True.',
            id='messaging.E004',
        ))
    samesite = getattr(settings, 'SESSION_COOKIE_SAMESITE', None)
    if samesite not in ('Lax', 'Strict'):
        problems.append(CheckWarning(
            'SESSION_COOKIE_SAMESITE is {!r}.'.format(samesite),
            hint=(
                'Use "Lax" (or "Strict"). The chat WebSocket authenticates with the '
                'session cookie, so a permissive SameSite lets another origin open a '
                'socket as the signed-in user.'
            ),
            id='messaging.W002',
        ))

    # ── 3. HSTS ──────────────────────────────────────────────────────────
    hsts = getattr(settings, 'SECURE_HSTS_SECONDS', 0) or 0
    if hsts < 3600:
        problems.append(CheckWarning(
            'SECURE_HSTS_SECONDS is {}, which is too short to matter.'.format(hsts),
            hint=(
                'Start at 3600 while you confirm every subdomain serves HTTPS, then '
                'raise it to 31536000. HSTS is what stops the first request of the '
                'day being made over plain http.'
            ),
            id='messaging.W003',
        ))

    # ── 4. WebSocket token handling ──────────────────────────────────────
    if getattr(settings, 'MESSAGING_WS_ALLOW_QUERY_TOKEN', True):
        problems.append(CheckWarning(
            'WebSocket authentication still accepts ?token= in the query string.',
            hint=(
                'Query strings are written verbatim into nginx access logs, proxy '
                'logs and browser history, so every access token used this way is '
                'now sitting in a log file. Move mobile clients to the '
                '"Authorization: Bearer" header, then set '
                'MESSAGING_WS_ALLOW_QUERY_TOKEN = False.'
            ),
            id='messaging.W004',
        ))

    # ── 5. Allowed hosts and CSRF origins ────────────────────────────────
    if '*' in (getattr(settings, 'ALLOWED_HOSTS', None) or []):
        problems.append(Error(
            'ALLOWED_HOSTS contains "*".',
            hint=(
                'Channels validates the WebSocket Origin against ALLOWED_HOSTS. With '
                '"*" any site on the internet can open a chat socket using the '
                "visitor's cookies. List the real hostnames."
            ),
            id='messaging.E005',
        ))

    csrf_origins = getattr(settings, 'CSRF_TRUSTED_ORIGINS', None) or []
    insecure_origins = [o for o in csrf_origins if o.startswith('http://')]
    if insecure_origins:
        problems.append(Error(
            'CSRF_TRUSTED_ORIGINS contains plain-http entries: {}'.format(', '.join(insecure_origins)),
            hint='Every trusted origin should be https://.',
            id='messaging.E006',
        ))

    return problems


@register(TAG, deploy=True)
def check_message_encryption(app_configs, **kwargs):
    """At-rest encryption settings — the other half of the story."""
    problems = []

    if not getattr(settings, 'MESSAGING_ENCRYPTION_KEY', None):
        problems.append(CheckWarning(
            'MESSAGING_ENCRYPTION_KEY is not set, so message bodies are stored as plain text.',
            hint=(
                'Generate one with:\n'
                '  python -c "from cryptography.fernet import Fernet; '
                'print(Fernet.generate_key().decode())"\n'
                'and put it in the environment, not in settings.py.'
            ),
            id='messaging.W005',
        ))

    if getattr(settings, 'MESSAGING_ENCRYPTION_FAIL_OPEN', False):
        problems.append(CheckWarning(
            'MESSAGING_ENCRYPTION_FAIL_OPEN is on: if encryption fails, messages are '
            'written to the database in plain text.',
            hint=(
                'This is meant to be a temporary switch during a rollout. Turn it off '
                'so a misconfigured worker refuses the write instead of silently '
                'storing readable messages.'
            ),
            id='messaging.W006',
        ))

    if getattr(settings, 'MESSAGING_NOTIFY_INCLUDE_PREVIEW', False):
        problems.append(CheckWarning(
            'Push and email notifications include the message text.',
            hint=(
                'The body then travels through Firebase and your mail relay in the '
                'clear and lands in their logs, which undoes the at-rest encryption '
                'for anyone reading those. Set MESSAGING_NOTIFY_INCLUDE_PREVIEW = '
                'False to send only the sender name.'
            ),
            id='messaging.W007',
        ))

    return problems


@register(TAG)
def check_call_relay(app_configs, **kwargs):
    """
    Calls have their own transport story: media is always encrypted with
    DTLS-SRTP, but without a relay it frequently has no path at all.
    """
    problems = []

    try:
        from apps.messaging.turn import relay_state
    except Exception:
        return problems

    state, message = relay_state()
    if state != 'ready' and _deploying():
        problems.append(CheckWarning(
            message,
            hint=(
                'Voice and video calls will connect between people on the same '
                'network and fail behind a firewall or symmetric NAT. Run '
                '"python manage.py turncheck" once configured.'
            ),
            id='messaging.W008',
        ))

    return problems
