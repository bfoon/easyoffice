"""
apps/messaging/turn.py
──────────────────────
ICE server configuration for the WebRTC voice/video calls.

WHY THIS FILE EXISTS
--------------------
"Peer-to-peer, no media server" is true for the AUDIO/VIDEO, but it is
NOT true for connectivity. STUN alone only works when at least one side
can be reached directly. Inside a UN compound — corporate firewall,
symmetric NAT, an explicit HTTP proxy, UDP blocked outbound — direct
paths fail and the call rings, connects, then dies after ~10 seconds
with iceConnectionState going ``checking → failed``.

The fix is a TURN relay. TURN is NOT a media server in the SFU/MCU
sense: it does not decode, mix, record, or transcode anything. It is a
dumb packet forwarder that both peers can reach, used ONLY when a direct
path can't be found. Media stays end-to-end encrypted (DTLS-SRTP) —
the relay cannot read it. Typically 10–25% of calls need it; the rest
still go direct.

You cannot skip it. There is no configuration of a pure-P2P setup that
survives symmetric NAT, because neither side has a reachable address.

TURN THAT SURVIVES A PROXY
--------------------------
Running TURN on the default UDP 3478 is what most guides show, and it is
exactly what a locked-down office network blocks. To survive:

    turns:turn.easyoffice.gm:443?transport=tcp

TLS on port 443 over TCP. To a middlebox this is indistinguishable from
an HTTPS connection, so it traverses nearly everything. Keep the UDP
listener too — UDP is much better for real-time media, so you want ICE
to PREFER it and fall back to 443/TCP only when it must. ICE does that
ordering automatically; you just have to offer both.

Note: a TURN server on 443 cannot share a port with nginx. Give coturn
its own IP or its own hostname on a second address.

SETTINGS
--------
    # settings.py
    WEBRTC_STUN_URLS = [
        "stun:stun.l.google.com:19302",
        "stun:turn.easyoffice.gm:3478",     # your own — don't rely on Google
    ]

    WEBRTC_TURN_HOSTS = [
        "turn:turn.easyoffice.gm:3478?transport=udp",
        "turn:turn.easyoffice.gm:3478?transport=tcp",
        "turns:turn.easyoffice.gm:443?transport=tcp",   # the proxy-buster
    ]

    # MUST match `static-auth-secret` in turnserver.conf. Keep it in an
    # env var / secrets manager, never in source control.
    WEBRTC_TURN_SECRET = os.environ.get("WEBRTC_TURN_SECRET", "")
    WEBRTC_TURN_TTL    = 3600      # credential lifetime, seconds

    # Set True only for testing "does TURN actually work?" — forces every
    # call through the relay by refusing host/srflx candidates.
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
    # Do NOT let clients relay to your internal network:
    no-multicast-peers
    denied-peer-ip=10.0.0.0-10.255.255.255
    denied-peer-ip=172.16.0.0-172.31.255.255
    denied-peer-ip=192.168.0.0-192.168.255.255
    # ...then re-allow nothing. An open TURN relay is an open proxy.
    external-ip=<public IP>/<private IP>     # only if behind 1:1 NAT

CREDENTIALS
-----------
coturn's ``use-auth-secret`` mode (RFC 7635 / the "TURN REST API") means
we never provision per-user TURN accounts. We mint a username of
``<unix-expiry>:<user-id>`` and an HMAC-SHA1 password derived from the
shared secret. coturn recomputes the same HMAC and accepts it until the
expiry passes. Credentials handed to the browser are therefore useless
an hour later, and revoking everything is one secret rotation.
"""

import base64
import hashlib
import hmac
import logging
import time

from django.conf import settings
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import JsonResponse
from django.views.decorators.cache import never_cache
from django.utils.decorators import method_decorator
from django.views.generic import View

log = logging.getLogger(__name__)


DEFAULT_STUN_URLS = [
    'stun:stun.l.google.com:19302',
    'stun:stun1.l.google.com:19302',
]


def _turn_credentials(user_id, ttl=None):
    """
    Mint a short-lived coturn REST-API credential pair.

    username = "<expiry-unix-timestamp>:<user-id>"
    password = base64(HMAC-SHA1(static_auth_secret, username))

    Returns (username, password, ttl) or (None, None, 0) when no secret is
    configured — in which case we degrade to STUN-only rather than
    handing the browser broken credentials.
    """
    secret = getattr(settings, 'WEBRTC_TURN_SECRET', '') or ''
    if not secret:
        return None, None, 0

    ttl = int(ttl or getattr(settings, 'WEBRTC_TURN_TTL', 3600))
    expiry = int(time.time()) + ttl
    username = f'{expiry}:{user_id}'

    digest = hmac.new(
        secret.encode('utf-8'),
        username.encode('utf-8'),
        hashlib.sha1,
    ).digest()

    return username, base64.b64encode(digest).decode('utf-8'), ttl


def build_ice_servers(user):
    """
    Assemble the ``iceServers`` array the browser feeds to RTCPeerConnection.

    Order matters less than you'd think (ICE probes all candidates in
    parallel and picks by priority), but keeping STUN first documents the
    intent: try direct, relay only as a last resort.
    """
    stun_urls = list(getattr(settings, 'WEBRTC_STUN_URLS', None) or DEFAULT_STUN_URLS)

    servers = []
    if stun_urls:
        servers.append({'urls': stun_urls})

    turn_hosts = list(getattr(settings, 'WEBRTC_TURN_HOSTS', None) or [])
    username, credential, ttl = _turn_credentials(getattr(user, 'id', 'anon'))

    if turn_hosts and username:
        servers.append({
            'urls': turn_hosts,
            'username': username,
            'credential': credential,
        })
    elif turn_hosts and not username:
        log.error(
            'WEBRTC_TURN_HOSTS is set but WEBRTC_TURN_SECRET is empty — '
            'serving STUN only. Calls WILL fail behind symmetric NAT.'
        )

    return servers, ttl


class IceServersView(LoginRequiredMixin, View):
    """
    GET /messages/webrtc/ice-servers/

    Response:
        {
            "ok": true,
            "ice_servers": [
                {"urls": ["stun:..."]},
                {"urls": ["turn:...","turns:..."],
                 "username": "1753812345:<uuid>", "credential": "..."}
            ],
            "ttl": 3600,
            "ice_transport_policy": "all",
            "has_turn": true
        }

    Never cached — the credentials expire, and a cached copy would hand a
    user a dead username an hour later. Fetch it immediately before
    creating the RTCPeerConnection, not once at page load.
    """

    @method_decorator(never_cache)
    def dispatch(self, *args, **kwargs):
        return super().dispatch(*args, **kwargs)

    def get(self, request):
        servers, ttl = build_ice_servers(request.user)

        force_relay = bool(getattr(settings, 'WEBRTC_FORCE_RELAY', False))
        has_turn = any('username' in s for s in servers)

        return JsonResponse({
            'ok': True,
            'ice_servers': servers,
            'ttl': ttl,
            # 'relay' makes the browser discard host/srflx candidates, so the
            # call can ONLY succeed through TURN. Use it to prove your relay
            # works; never ship it on, it wastes bandwidth on every call.
            'ice_transport_policy': 'relay' if force_relay else 'all',
            'has_turn': has_turn,
        })
