"""
apps/messaging/management/commands/turncheck.py
──────────────────────────────────────────────
Prove the TURN relay actually works, from this machine, with these
credentials.

    python manage.py turncheck
    python manage.py turncheck --host turn.easyoffice.gm --port 443 --tls

"Calls fail from the office" has three separate causes that look identical
from a browser: no relay configured, a relay configured with the wrong
shared secret, and a relay that is fine but unreachable on the port the
office allows out. This command tells them apart by speaking TURN: it
sends a real Allocate request (RFC 5766) with the same HMAC credentials
the browser gets, and reports what came back.

A pass here plus a failing call means the problem is on the client's
network, not the server. A fail here means fix the server first.
"""

import hashlib
import hmac
import os
import re
import socket
import ssl
import struct
import time

from django.core.management.base import BaseCommand, CommandError

from apps.messaging.turn import turn_settings, turn_credentials, relay_state

MAGIC_COOKIE = 0x2112A442

METHOD_ALLOCATE = 0x003
CLASS_REQUEST = 0x00
MSG_ALLOCATE_REQUEST = 0x0003
MSG_ALLOCATE_SUCCESS = 0x0103
MSG_ALLOCATE_ERROR = 0x0113

ATTR_USERNAME = 0x0006
ATTR_MESSAGE_INTEGRITY = 0x0008
ATTR_ERROR_CODE = 0x0009
ATTR_REALM = 0x0014
ATTR_NONCE = 0x0015
ATTR_XOR_RELAYED_ADDRESS = 0x0016
ATTR_REQUESTED_TRANSPORT = 0x0019
ATTR_SOFTWARE = 0x8022

TRANSPORT_UDP = 17


def _pad4(n):
    return (4 - (n % 4)) % 4


def _attr(attr_type, value):
    return struct.pack('!HH', attr_type, len(value)) + value + (b'\x00' * _pad4(len(value)))


def _header(msg_type, txid, body_len):
    return struct.pack('!HHI', msg_type, body_len, MAGIC_COOKIE) + txid


def _build_allocate(txid, username=None, realm=None, nonce=None, key=None):
    """
    Build an Allocate request. With ``key`` supplied the message is signed
    with MESSAGE-INTEGRITY, which is what coturn requires on the second
    attempt after it has challenged us with a realm and nonce.
    """
    body = _attr(ATTR_REQUESTED_TRANSPORT, struct.pack('!BBBB', TRANSPORT_UDP, 0, 0, 0))
    body += _attr(ATTR_SOFTWARE, b'easyoffice-turncheck')

    if key is None:
        return _header(MSG_ALLOCATE_REQUEST, txid, len(body)) + body

    body += _attr(ATTR_USERNAME, username.encode('utf-8'))
    body += _attr(ATTR_REALM, realm)
    body += _attr(ATTR_NONCE, nonce)

    # The length in the header must already account for the 24-byte
    # MESSAGE-INTEGRITY attribute when the HMAC is computed over the
    # message. Getting this wrong is the usual reason a hand-rolled STUN
    # client gets 401 forever.
    length_with_mi = len(body) + 24
    to_sign = _header(MSG_ALLOCATE_REQUEST, txid, length_with_mi) + body
    digest = hmac.new(key, to_sign, hashlib.sha1).digest()
    body += _attr(ATTR_MESSAGE_INTEGRITY, digest)

    return _header(MSG_ALLOCATE_REQUEST, txid, len(body)) + body


def _parse_attrs(payload):
    attrs, i = {}, 0
    while i + 4 <= len(payload):
        atype, alen = struct.unpack('!HH', payload[i:i + 4])
        i += 4
        value = payload[i:i + alen]
        i += alen + _pad4(alen)
        attrs.setdefault(atype, value)
    return attrs


def _decode_error(value):
    if len(value) < 4:
        return 0, ''
    cls, number = value[2], value[3]
    return cls * 100 + number, value[4:].decode('utf-8', 'replace')


def _decode_xor_address(value):
    if len(value) < 8:
        return ''
    family = value[1]
    port = struct.unpack('!H', value[2:4])[0] ^ (MAGIC_COOKIE >> 16)
    if family == 0x01:
        raw = struct.unpack('!I', value[4:8])[0] ^ MAGIC_COOKIE
        return '{}.{}.{}.{}:{}'.format(
            (raw >> 24) & 0xFF, (raw >> 16) & 0xFF, (raw >> 8) & 0xFF, raw & 0xFF, port
        )
    return '[ipv6]:{}'.format(port)


def _long_term_key(username, realm, password):
    return hashlib.md5(
        '{}:{}:{}'.format(username, realm.decode('utf-8', 'replace'), password).encode('utf-8')
    ).digest()


def _read_message(sock):
    """Read exactly one STUN message off a stream socket."""
    head = b''
    while len(head) < 20:
        chunk = sock.recv(20 - len(head))
        if not chunk:
            raise ConnectionError('the relay closed the connection before replying')
        head += chunk
    msg_type, body_len = struct.unpack('!HH', head[:4])
    txid = head[8:20]
    body = b''
    while len(body) < body_len:
        chunk = sock.recv(body_len - len(body))
        if not chunk:
            raise ConnectionError('the relay closed the connection mid-message')
        body += chunk
    return msg_type, txid, _parse_attrs(body)


def _parse_turn_url(url):
    """
    Pull host, port and transport out of a turn: or turns: URL.

        turns:turn.example.gm:443?transport=tcp -> ('turn.example.gm', 443, 'tcp', True)
    """
    m = re.match(r'^(turns?):([^:?/]+)(?::(\d+))?(?:\?transport=(\w+))?$', url.strip())
    if not m:
        return None
    scheme, host, port, transport = m.groups()
    tls = (scheme == 'turns')
    transport = (transport or ('tcp' if tls else 'udp')).lower()
    port = int(port) if port else (5349 if tls else 3478)
    return host, port, transport, tls


class Command(BaseCommand):
    help = 'Send a real TURN Allocate request to prove the relay works.'

    def add_arguments(self, parser):
        parser.add_argument('--host', help='Override the TURN host to test.')
        parser.add_argument('--port', type=int, help='Override the port.')
        parser.add_argument('--tls', action='store_true', help='Use TLS (turns:).')
        parser.add_argument('--timeout', type=float, default=6.0, help='Socket timeout in seconds.')
        parser.add_argument(
            '--insecure', action='store_true',
            help="Don't verify the relay's TLS certificate (diagnosis only).",
        )

    # ── output helpers ───────────────────────────────────────────────────
    def _ok(self, msg):
        self.stdout.write(self.style.SUCCESS('  PASS  ' + msg))

    def _fail(self, msg):
        self.stdout.write(self.style.ERROR('  FAIL  ' + msg))

    def _info(self, msg):
        self.stdout.write('        ' + msg)

    def handle(self, *args, **opts):
        hosts, secret, ttl, realm_setting = turn_settings()
        state, message = relay_state()

        self.stdout.write('')
        self.stdout.write(self.style.MIGRATE_HEADING('TURN relay check'))
        self.stdout.write('')

        # ── 1. configuration ─────────────────────────────────────────────
        self.stdout.write('Configuration')
        if state != 'ready':
            self._fail(message)
            self.stdout.write('')
            raise CommandError('Fix the configuration before testing connectivity.')
        self._ok('WEBRTC_TURN_HOSTS and WEBRTC_TURN_SECRET are both set.')
        self._info('credential lifetime: {}s'.format(ttl))

        if not any(u.startswith('turns:') for u in hosts):
            self.stdout.write(self.style.WARNING(
                '  WARN  No turns: URL on 443. Relay-over-TLS on 443 is the only\n'
                '        thing that reliably crosses a corporate HTTP proxy — add\n'
                '        "turns:<host>:443?transport=tcp" or office calls will\n'
                '        still fail even with this relay running.'
            ))
        self.stdout.write('')

        # ── 2. pick a target ─────────────────────────────────────────────
        if opts['host']:
            target = (opts['host'], opts['port'] or (443 if opts['tls'] else 3478),
                      'tcp', bool(opts['tls']))
        else:
            target = None
            for url in hosts:
                parsed = _parse_turn_url(url)
                if parsed and parsed[2] == 'tcp':
                    target = parsed
                    break
            if target is None:
                for url in hosts:
                    parsed = _parse_turn_url(url)
                    if parsed:
                        target = parsed
                        break
            if target is None:
                raise CommandError('No TURN URL in WEBRTC_TURN_HOSTS could be parsed.')

        host, port, transport, tls = target
        if transport == 'udp':
            self.stdout.write(self.style.WARNING(
                '  WARN  Only a UDP URL was found. This check speaks TCP/TLS, which is\n'
                '        the transport that matters behind a proxy. Testing TCP on the\n'
                '        same host and port instead.'
            ))
        self.stdout.write('Reaching {}:{} over {}'.format(host, port, 'TLS' if tls else 'TCP'))

        # ── 3. connect ───────────────────────────────────────────────────
        started = time.time()
        try:
            raw = socket.create_connection((host, port), timeout=opts['timeout'])
        except OSError as exc:
            self._fail('Could not open a socket: {}'.format(exc))
            self._info('The relay is not listening here, or outbound traffic to this')
            self._info('port is blocked from this machine.')
            raise CommandError('Relay unreachable.')

        sock = raw
        if tls:
            ctx = ssl.create_default_context()
            if opts['insecure']:
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            try:
                sock = ctx.wrap_socket(raw, server_hostname=host)
            except ssl.SSLError as exc:
                self._fail('TLS handshake failed: {}'.format(exc))
                self._info("The relay's certificate does not match {}, or it has".format(host))
                self._info('expired. Check cert= and pkey= in turnserver.conf.')
                raise CommandError('TLS handshake failed.')
            self._ok('TLS handshake completed ({}).'.format(sock.version()))

        sock.settimeout(opts['timeout'])
        self._ok('Connected in {:.0f} ms.'.format((time.time() - started) * 1000))

        # ── 4. speak TURN ────────────────────────────────────────────────
        try:
            username, password, _ttl = turn_credentials('turncheck')

            # First Allocate is expected to be refused with a challenge.
            txid = os.urandom(12)
            sock.sendall(_build_allocate(txid))
            msg_type, _txid, attrs = _read_message(sock)

            if msg_type == MSG_ALLOCATE_SUCCESS:
                self._fail('The relay allocated WITHOUT any credentials.')
                self._info('This relay is an OPEN PROXY. Anyone on the internet can')
                self._info('use it to relay traffic and it will be abused. Set')
                self._info('use-auth-secret and static-auth-secret in turnserver.conf')
                self._info('immediately.')
                raise CommandError('Relay accepts unauthenticated allocations.')

            if msg_type != MSG_ALLOCATE_ERROR:
                self._fail('Unexpected reply type 0x{:04x} — is this really a TURN server?'.format(msg_type))
                raise CommandError('Not a TURN server.')

            code, reason = _decode_error(attrs.get(ATTR_ERROR_CODE, b''))
            realm = attrs.get(ATTR_REALM)
            nonce = attrs.get(ATTR_NONCE)

            if code != 401 or not realm or not nonce:
                self._fail('Expected a 401 challenge, got {} {}'.format(code, reason))
                raise CommandError('Unexpected challenge.')

            realm_text = realm.decode('utf-8', 'replace')
            self._ok('Relay challenged us, realm "{}".'.format(realm_text))
            if realm_setting and realm_setting != realm_text:
                self.stdout.write(self.style.WARNING(
                    '  WARN  WEBRTC_TURN_REALM is "{}" but the relay says "{}".'.format(
                        realm_setting, realm_text)
                ))

            # Second Allocate, signed.
            key = _long_term_key(username, realm, password)
            txid = os.urandom(12)
            sock.sendall(_build_allocate(txid, username, realm, nonce, key))
            msg_type, _txid, attrs = _read_message(sock)

            if msg_type == MSG_ALLOCATE_SUCCESS:
                relayed = _decode_xor_address(attrs.get(ATTR_XOR_RELAYED_ADDRESS, b''))
                self._ok('Allocate succeeded. Relayed address: {}'.format(relayed or 'unknown'))
                self.stdout.write('')
                self.stdout.write(self.style.SUCCESS(
                    'The relay works and the shared secret matches. If calls still fail,\n'
                    'the problem is on the caller\'s network — have them open the call\n'
                    'window, press Settings, and send you the connection report.'
                ))
                self.stdout.write('')
                return

            code, reason = _decode_error(attrs.get(ATTR_ERROR_CODE, b''))
            if code == 401:
                self._fail('Credentials rejected (401 {}).'.format(reason))
                self._info('WEBRTC_TURN_SECRET does not match static-auth-secret in')
                self._info('turnserver.conf. They must be byte-for-byte identical.')
            elif code == 486:
                self._fail('Allocation quota reached (486). The relay is at capacity.')
                self._info('Raise total-quota in turnserver.conf.')
            elif code == 508:
                self._fail('Insufficient capacity (508). The relay has no ports left.')
            else:
                self._fail('Allocate refused: {} {}'.format(code, reason))
            raise CommandError('Allocate failed.')

        except (socket.timeout, TimeoutError):
            self._fail('The relay accepted the connection but never replied.')
            self._info('Something is listening on this port but it is not coturn, or')
            self._info('a middlebox is swallowing the STUN payload.')
            raise CommandError('No reply from the relay.')
        except ConnectionError as exc:
            self._fail(str(exc))
            raise CommandError('Connection dropped.')
        finally:
            try:
                sock.close()
            except OSError:
                pass
