"""
apps/messaging/turn.py
──────────────────────
ICE server configuration for WebRTC voice/video calls.

WHY THIS FILE EXISTS
--------------------
"Peer-to-peer, no media server" is true of the AUDIO and VIDEO. It is not
true of CONNECTIVITY. STUN only tells a peer its own public address; it
cannot make that peer reachable. Inside a UN compound — corporate
firewall, symmetric NAT, an explicit HTTP proxy, UDP blocked outbound —
neither side has an address the other can use. ICE goes
``checking → failed`` and the call rings, connects, then dies around the
ten-second mark.

A TURN relay fixes that. TURN is not a media server in the SFU/MCU sense:
it does not decode, mix, record or transcode. It is a dumb packet
forwarder both peers can reach, used only when no direct path exists.
Media stays end-to-end encrypted (DTLS-SRTP) and the relay cannot read
it. Typically 10–25% of calls need it; the rest still go direct.

There is no configuration of a pure-P2P setup that survives symmetric
NAT, because neither side has a reachable address. You cannot skip this.

TURN THAT SURVIVES A PROXY
--------------------------
The default UDP 3478 listener is what most guides show and exactly what a
locked-down office blocks. What gets through is:

    turns:turn.easyoffice.gm:443?transport=tcp

TLS on 443 over TCP is indistinguishable from HTTPS to a middlebox. Keep
the UDP listener too — UDP is much better for real-time media, so you
want ICE to PREFER it and fall back to 443/TCP only when it must. ICE
does that ordering itself; you only have to offer both.

A TURN server on 443 cannot share the port with nginx. Give coturn its
own IP or its own hostname on a second address.

SETTINGS
--------
    WEBRTC_STUN_URLS = [
        "stun:stun.l.google.com:19302",
        "stun:turn.easyoffice.gm:3478",     # your own — don't rely on Google
    ]

    WEBRTC_TURN_HOSTS = [
        "turn:turn.easyoffice.gm:3478?transport=udp",
        "turn:turn.easyoffice.gm:3478?transport=tcp",
        "turns:turn.easyoffice.gm:443?transport=tcp",   # the proxy-buster
    ]

    # MUST match `static-auth-secret` in turnserver.conf. Env var or
    # secrets manager, never source control.
    WEBRTC_TURN_SECRET = os.environ.get("WEBRTC_TURN_SECRET", "")
    WEBRTC_TURN_TTL    = 3600      # credential lifetime, seconds
    WEBRTC_TURN_REALM  = "easyoffice.gm"   # must match coturn's `realm`

    # Force every call through the relay. Use to prove the relay works;
    # never ship it on, it wastes bandwidth on every call.
    WEBRTC_FORCE_RELAY = False

COTURN
------
    apt install coturn
    # /etc/turnserver.conf
    listening-port=3478
    tls-listening-port=443
    fingerprint
    use-auth-secret
    static-auth-secret=<same value as WEBRTC_TURN_SECRET>
    realm=easyoffice.gm
    total-quota=100
    stale-nonce=600
    cert=/etc/letsencrypt/live/turn.easyoffice.gm/fullchain.pem
    pkey=/etc/letsencrypt/live/turn.easyoffice.gm/privkey.pem
    no-tlsv1
    no-tlsv1_1
    # Never let a client relay into your internal network. An open TURN
    # relay is an open proxy.
    no-multicast-peers
    denied-peer-ip=10.0.0.0-10.255.255.255
    denied-peer-ip=172.16.0.0-172.31.255.255
    denied-peer-ip=192.168.0.0-192.168.255.255
    external-ip=<public IP>/<private IP>     # only if behind 1:1 NAT

CREDENTIALS
-----------
coturn's ``use-auth-secret`` mode (the "TURN REST API") means no per-user
TURN accounts. We mint a username of ``<unix-expiry>:<user-id>`` and an
HMAC-SHA1 password derived from the shared secret. coturn recomputes the
same HMAC and accepts it until the expiry passes, so credentials handed
to a browser are useless an hour later and revoking everything is one
secret rotation.

WHAT CHANGED IN THIS VERSION
----------------------------
The endpoint now reports WHY there is no relay, not just that there
isn't one. Previously a missing ``WEBRTC_TURN_SECRET`` and a missing
``WEBRTC_TURN_HOSTS`` both surfaced to the browser as ``has_turn: false``,
and the call window logged a single console line nobody read. The popup
now shows the state in its header and its connection panel, so "calls
don't work from the office" stops being a mystery. Run
``python manage.py turncheck`` to test the relay for real.
"""

import base64
import hashlib
import hmac
import logging
import time

from django.conf import settings
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import JsonResponse
from django.utils.decorators import method_decorator
from django.views.decorators.cache import never_cache
from django.views.generic import View

log = logging.getLogger(__name__)


DEFAULT_STUN_URLS = [
    'stun:stun.l.google.com:19302',
    'stun:stun1.l.google.com:19302',
]

# Credentials shorter than this are pointless (a call outlives them) and
# longer than a day defeats the purpose of short-lived credentials.
_TTL_MIN = 300
_TTL_MAX = 86400


# ─────────────────────────────────────────────────────────────────────────────
# Configuration introspection
# ─────────────────────────────────────────────────────────────────────────────

def turn_settings():
    """
    Return ``(hosts, secret, ttl, realm)`` from settings, normalised.

    Kept separate from :func:`build_ice_servers` so the management command
    and the system check read exactly what the view reads.
    """
    hosts = [h for h in (getattr(settings, 'WEBRTC_TURN_HOSTS', None) or []) if h]
    secret = (getattr(settings, 'WEBRTC_TURN_SECRET', '') or '').strip()
    realm = (getattr(settings, 'WEBRTC_TURN_REALM', '') or '').strip()

    try:
        ttl = int(getattr(settings, 'WEBRTC_TURN_TTL', 3600))
    except (TypeError, ValueError):
        ttl = 3600
    ttl = max(_TTL_MIN, min(_TTL_MAX, ttl))

    return hosts, secret, ttl, realm


def relay_state():
    """
    Say precisely what is wrong with the relay configuration.

    Returns ``(state, human_message)`` where state is one of:

        ``ready``       hosts and secret are both present
        ``no_hosts``    no TURN URLs configured at all
        ``no_secret``   URLs configured but no shared secret to sign with
    """
    hosts, secret, _ttl, _realm = turn_settings()

    if not hosts:
        return 'no_hosts', (
            'No TURN relay is configured. Calls will work between people on '
            'the same network and fail behind a firewall or symmetric NAT. '
            'Set WEBRTC_TURN_HOSTS and WEBRTC_TURN_SECRET.'
        )
    if not secret:
        return 'no_secret', (
            'WEBRTC_TURN_HOSTS is set but WEBRTC_TURN_SECRET is empty, so no '
            'credentials can be signed and the relay will reject every '
            'allocation. The secret must match static-auth-secret in '
            'turnserver.conf.'
        )
    return 'ready', 'A TURN relay is configured.'


# ─────────────────────────────────────────────────────────────────────────────
# Credentials
# ─────────────────────────────────────────────────────────────────────────────

def turn_credentials(user_id, ttl=None):
    """
    Mint a short-lived coturn REST-API credential pair.

        username = "<expiry-unix-timestamp>:<user-id>"
        password = base64(HMAC-SHA1(static_auth_secret, username))

    Returns ``(username, password, ttl)``, or ``(None, None, 0)`` when no
    secret is configured — in which case we degrade to STUN only rather
    than handing the browser credentials the relay will refuse.
    """
    _hosts, secret, default_ttl, _realm = turn_settings()
    if not secret:
        return None, None, 0

    try:
        ttl = int(ttl) if ttl else default_ttl
    except (TypeError, ValueError):
        ttl = default_ttl
    ttl = max(_TTL_MIN, min(_TTL_MAX, ttl))

    expiry = int(time.time()) + ttl
    username = f'{expiry}:{user_id}'

    digest = hmac.new(
        secret.encode('utf-8'),
        username.encode('utf-8'),
        hashlib.sha1,
    ).digest()

    return username, base64.b64encode(digest).decode('utf-8'), ttl


# Backwards-compatible alias — the old private name is imported in a few
# places and by anyone who copied the previous version of this file.
_turn_credentials = turn_credentials


def build_ice_servers(user):
    """
    Assemble the ``iceServers`` array the browser feeds RTCPeerConnection.

    STUN first documents the intent: try direct, relay only as a last
    resort. ICE probes candidates in parallel and orders them by priority,
    so the ordering here is documentation rather than control.
    """
    stun_urls = list(getattr(settings, 'WEBRTC_STUN_URLS', None) or DEFAULT_STUN_URLS)
    hosts, _secret, _ttl, _realm = turn_settings()

    servers = []
    if stun_urls:
        servers.append({'urls': stun_urls})

    username, credential, ttl = turn_credentials(getattr(user, 'id', 'anon'))

    if hosts and username:
        servers.append({
            'urls': hosts,
            'username': username,
            'credential': credential,
        })
    elif hosts and not username:
        log.error(
            'WEBRTC_TURN_HOSTS is set but WEBRTC_TURN_SECRET is empty — '
            'serving STUN only. Calls WILL fail behind symmetric NAT.'
        )

    return servers, ttl


class IceServersView(LoginRequiredMixin, View):
    """
    GET /messages/webrtc/ice-servers/

    Response::

        {
            "ok": true,
            "ice_servers": [
                {"urls": ["stun:..."]},
                {"urls": ["turn:...", "turns:..."],
                 "username": "1753812345:<uuid>", "credential": "..."}
            ],
            "ttl": 3600,
            "ice_transport_policy": "all",
            "has_turn": true,
            "relay_state": "ready",
            "relay_message": "A TURN relay is configured."
        }

    Never cached. The credentials expire, and a cached copy would hand a
    user a dead username an hour later — fetch it immediately before
    creating the RTCPeerConnection, not once at page load.
    """

    @method_decorator(never_cache)
    def dispatch(self, *args, **kwargs):
        return super().dispatch(*args, **kwargs)

    def get(self, request):
        servers, ttl = build_ice_servers(request.user)
        state, message = relay_state()
        has_turn = any('username' in s for s in servers)
        force_relay = bool(getattr(settings, 'WEBRTC_FORCE_RELAY', False))

        return JsonResponse({
            'ok': True,
            'ice_servers': servers,
            'ttl': ttl,
            # 'relay' makes the browser discard host and srflx candidates so
            # the call can ONLY succeed through TURN. Useful to prove the
            # relay works; never ship it on.
            'ice_transport_policy': 'relay' if force_relay else 'all',
            'has_turn': has_turn,
            'relay_state': state if not has_turn else 'ready',
            'relay_message': message,
        })
