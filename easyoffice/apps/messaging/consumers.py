"""
apps/messaging/consumers.py
───────────────────────────
WHAT CHANGED IN THIS VERSION (three reported bugs)
--------------------------------------------------

1. "Messages to a group just disappear."
   Every failure in the save path used to `return None`, and the client
   had already cleared the composer. The message evaporated with no error
   anywhere the user could see. The socket now ALWAYS answers a
   chat_message frame — either with the broadcast, or with an explicit
   {"type": "send_error", "reason": "...", "client_id": "..."} frame that
   the composer uses to put the text back and show why. Unexpected
   exceptions are caught and reported the same way instead of tearing
   down the socket mid-send.

2. "Someone is typing" with nobody typing.
   views.py::_broadcast_presence_update reuses the 'chat.typing' event to
   carry a presence heartbeat, and views.py::_broadcast_typing nests its
   fields under 'payload'. chat_typing only read TOP-LEVEL keys, so both
   arrived as a typing event with an EMPTY sender_id. The client's
   self-filter (`'' !== my_id`) passed, and every heartbeat from a DM
   partner painted "Someone is typing…". chat_typing now normalises both
   shapes, forwards a presence payload as presence_update (its real
   type), and DROPS any typing event with no sender_id.

3. "Calls stop on the caller's side and the callee never rings."
   The caller's popup emits call_offer / ice_candidate immediately, but
   the callee's popup does not exist until they accept the ring — the
   chat page ignores those signals, so the SDP offer was thrown away and
   the callee's popup came up to silence. The offer and the caller's
   early ICE candidates are now stashed for 90s and REPLAYED to the room
   the moment a peer announces call_callee_ready.
"""

import json
import logging

from asgiref.sync import sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async
from django.core.cache import cache
from django.utils import timezone

log = logging.getLogger(__name__)


# 🔒 SECURITY: per-signal-type whitelist of client-supplied keys we will
# relay to the peer. The old code did `payload = dict(data)` and forwarded
# ARBITRARY client JSON verbatim — a malicious peer could inject any field
# into the other client's handler. Identity fields (sender_id, sender_name,
# room_id) are ALWAYS set server-side and can never be spoofed.
_SIGNAL_ALLOWED_KEYS = {
    'call_offer':           {'sdp', 'call_kind'},
    'call_answer':          {'sdp'},
    'ice_candidate':        {'candidate'},
    'call_hangup':          set(),
    'call_decline':         set(),
    'call_cancel':          set(),
    # 🩹 FIX: 'ice_restart' and 'share_only' were being stripped by this
    # whitelist. Without 'ice_restart', the peer treated every ICE-restart
    # renegotiation as a video upgrade and grabbed the camera mid-voice-call.
    # 'share_only' lets a voice-call peer render an incoming screen share
    # without being forced to turn on their own camera.
    'call_upgrade_offer':   {'sdp', 'ice_restart', 'share_only'},
    'call_upgrade_answer':  {'sdp'},
    'call_callee_ready':    set(),
    # 🩹 FIX: the client sends 'num_pages' (not 'total_pages'); it was being
    # stripped, so the viewer always saw "1 / 1" and couldn't page forward.
    'present_start':        {'url', 'kind', 'title', 'filename', 'page', 'total_pages', 'num_pages'},
    'present_end':          set(),
    'present_page':         {'page'},
    'present_request_page': {'page'},
}

# ── Call handshake replay ────────────────────────────────────────────────
# How long a stashed offer stays replayable, and how many early ICE
# candidates we keep. 90s comfortably covers "phone rings, user picks up".
_CALL_STASH_TTL = 90
_CALL_STASH_MAX_CANDIDATES = 60

# Signals that mean the call is over — drop the stash so a NEW call in the
# same room never replays a dead offer.
_CALL_TEARDOWN = {'call_hangup', 'call_decline', 'call_cancel'}


def _offer_key(room_id):
    return f'call:offer:{room_id}'


def _ice_key(room_id):
    return f'call:ice:{room_id}'


class ChatConsumer(AsyncWebsocketConsumer):

    async def connect(self):
        self.room_id = self.scope['url_route']['kwargs']['room_id']
        self.room_group = f'chat_{self.room_id}'
        # ws_auth.JWTAuthMiddleware guarantees this key exists now.
        self.user = self.scope['user']

        if not self.user.is_authenticated:
            await self.close()
            return

        allowed = await self.user_in_room()
        if not allowed:
            await self.close()
            return

        await self.channel_layer.group_add(self.room_group, self.channel_name)
        await self.accept()

        # ✅ READ RECEIPTS: the moment this socket joins the group, every
        # message already in the room has physically reached a live client
        # for this user — that is exactly what "delivered" (✓✓ grey) means.
        # Fires a chat.receipt broadcast so the sender's ticks update live.
        await self._mark_delivered()

    async def disconnect(self, code):
        try:
            await self.channel_layer.group_discard(self.room_group, self.channel_name)
        except Exception:
            pass

    # --------------------------------------------------
    # RECEIVE
    # --------------------------------------------------

    async def receive(self, text_data=None, bytes_data=None):
        """
        Thin wrapper so an unexpected exception NEVER kills the socket
        silently. A dead socket used to look, from the composer, exactly
        like a message that "just disappeared".
        """
        try:
            await self._receive(text_data, bytes_data)
        except Exception:
            log.exception(
                'ChatConsumer.receive failed (room=%s user=%s)',
                getattr(self, 'room_id', '?'), getattr(self.user, 'pk', '?'),
            )
            await self._send_error(
                'Something went wrong on the server. Your message was not sent.',
                code='server_error',
                client_id=self._last_client_id,
            )

    _last_client_id = ''

    async def _receive(self, text_data=None, bytes_data=None):
        if not text_data:
            return

        try:
            data = json.loads(text_data)
        except Exception:
            return

        user = self.user
        if not user.is_authenticated:
            return

        msg_type = data.get('type', 'chat_message')

        # Optimistic-UI correlation id. The composer sends one with every
        # message so it can match an error frame back to the exact bubble.
        client_id = str(data.get('client_id') or '')
        self._last_client_id = client_id

        # -------------------------
        # TYPING
        # -------------------------
        if msg_type == 'typing':
            # 🔒 Re-verify membership so a user removed from the room
            # mid-connection can't keep signalling into it.
            if not await self.user_in_room():
                await self.close()
                return
            await self.channel_layer.group_send(
                self.room_group,
                {
                    'type': 'chat.typing',
                    'room_id': str(self.room_id),
                    'sender_id': str(user.id),
                    'sender_name': self._safe_full_name(user),
                    'sender_initials': self._safe_initials(user),
                    'sender_avatar_url': await self._avatar_url(user),
                }
            )
            return

        # -------------------------
        # READ RECEIPT
        # -------------------------
        # Client sends {"type": "read"} when the tab is visible and the
        # newest bubble is on screen, optionally with {"up_to": "<msg id>"}
        # to be precise about how far the user has actually scrolled.
        # Doing this over the socket avoids an HTTP round-trip per read.
        if msg_type == 'read':
            if not await self.user_in_room():
                await self.close()
                return
            await self._mark_read(data.get('up_to') or None)
            return

        # -------------------------
        # VOICE-CALL SIGNALING
        # -------------------------
        # WebRTC needs to exchange SDP offers/answers and ICE candidates
        # between the two peers. We relay whitelisted fields through the
        # room group — the server never stores them, and the actual audio
        # stream is peer-to-peer (not through the server).
        if msg_type in _SIGNAL_ALLOWED_KEYS:
            # Must be a DM room — enforce 1-on-1 scope.
            is_direct = await self.room_is_direct()
            if not is_direct:
                return

            # 🔒 Re-verify membership on every signal (cheap query, and
            # signalling is low-frequency). Prevents a removed member from
            # continuing an in-progress call handshake.
            if not await self.user_in_room():
                await self.close()
                return

            # 🔒 Build payload from the whitelist only — never dict(data).
            allowed = _SIGNAL_ALLOWED_KEYS[msg_type]
            payload = {k: data[k] for k in allowed if k in data}
            payload['type']        = msg_type
            payload['sender_id']   = str(user.id)              # server-set
            payload['sender_name'] = self._safe_full_name(user)  # server-set
            payload['room_id']     = str(self.room_id)          # server-set

            await self.channel_layer.group_send(
                self.room_group,
                {
                    'type': 'chat.signal',
                    'payload': payload,
                    'sender_channel': self.channel_name,
                }
            )

            # 🩹 CALL FIX — see module docstring. The caller emits its offer
            # (and starts trickling ICE) while the callee is still looking
            # at a ring; the callee's popup does not exist yet, so those
            # frames used to land nowhere and the call died on the caller's
            # side. Stash them, replay them when the callee announces
            # itself.
            await self._handle_call_stash(msg_type, payload)
            return

        # -------------------------
        # ONLY HANDLE TEXT HERE
        # (polls handled via views)
        # -------------------------
        if msg_type != 'chat_message':
            return

        message = (data.get('message') or data.get('content') or '').strip()
        if not message:
            await self._send_error('Empty message.', code='empty', client_id=client_id)
            return

        # Soft cap, mirroring the REST send path.
        if len(message) > 5000:
            message = message[:5000]

        reply_to_id = data.get('reply_to') or None

        result = await self.save_and_build_payload(user, message, reply_to_id)

        # 🩹 MESSAGE FIX — every failure now comes back to the sender with a
        # reason instead of vanishing.
        if not result.get('ok'):
            await self._send_error(
                result.get('error') or 'Your message could not be sent.',
                code=result.get('code') or 'save_failed',
                client_id=client_id,
            )
            return

        payload = result['payload']
        if client_id:
            payload = dict(payload, client_id=client_id)

        await self.channel_layer.group_send(
            self.room_group,
            {
                'type': 'chat.message',
                'payload': payload,
            }
        )

    # --------------------------------------------------
    # ERROR CHANNEL (sender-only)
    # --------------------------------------------------

    async def _send_error(self, message, code='error', client_id=''):
        """
        Tell THIS socket (only) that something it asked for failed.
        The composer listens for 'send_error' and restores the text.
        """
        try:
            await self.send(text_data=json.dumps({
                'type': 'send_error',
                'code': code,
                'error': message,
                'room_id': str(getattr(self, 'room_id', '')),
                'client_id': client_id or '',
            }))
        except Exception:
            pass

    # --------------------------------------------------
    # CALL HANDSHAKE STASH / REPLAY
    # --------------------------------------------------

    async def _handle_call_stash(self, msg_type, payload):
        try:
            if msg_type == 'call_offer':
                await sync_to_async(cache.set)(
                    _offer_key(self.room_id),
                    {'payload': payload, 'sender_channel': self.channel_name},
                    _CALL_STASH_TTL,
                )
                await sync_to_async(cache.delete)(_ice_key(self.room_id))
                return

            if msg_type == 'ice_candidate':
                stash = await sync_to_async(cache.get)(_offer_key(self.room_id))
                # Only the offerer's early candidates are worth replaying;
                # the answerer's peer is already listening by then.
                if not stash or stash.get('sender_channel') != self.channel_name:
                    return
                pending = await sync_to_async(cache.get)(_ice_key(self.room_id)) or []
                if len(pending) < _CALL_STASH_MAX_CANDIDATES:
                    pending.append(payload)
                    await sync_to_async(cache.set)(
                        _ice_key(self.room_id), pending, _CALL_STASH_TTL,
                    )
                return

            if msg_type in _CALL_TEARDOWN or msg_type == 'call_answer':
                # Answered or over — the replay window is closed either way.
                await sync_to_async(cache.delete)(_offer_key(self.room_id))
                await sync_to_async(cache.delete)(_ice_key(self.room_id))
                return

            if msg_type == 'call_callee_ready':
                stash = await sync_to_async(cache.get)(_offer_key(self.room_id))
                if not stash:
                    return
                # Replay to the group, attributed to the ORIGINAL caller's
                # channel so the caller does not receive its own offer back.
                origin = stash.get('sender_channel')
                await self.channel_layer.group_send(
                    self.room_group,
                    {
                        'type': 'chat.signal',
                        'payload': stash['payload'],
                        'sender_channel': origin,
                    },
                )
                for cand in (await sync_to_async(cache.get)(_ice_key(self.room_id)) or []):
                    await self.channel_layer.group_send(
                        self.room_group,
                        {
                            'type': 'chat.signal',
                            'payload': cand,
                            'sender_channel': origin,
                        },
                    )
                log.info('call: replayed stashed offer to room %s', self.room_id)
        except Exception:
            # A broken cache must never break signalling itself.
            log.exception('call stash/replay failed for room %s', self.room_id)

    # --------------------------------------------------
    # SEND EVENTS
    # --------------------------------------------------

    async def chat_message(self, event):
        await self.send(text_data=json.dumps(event['payload']))

    async def chat_reaction(self, event):
        await self.send(text_data=json.dumps(event['payload']))

    async def chat_poll(self, event):
        await self.send(text_data=json.dumps(event['payload']))

    async def chat_edit(self, event):
        await self.send(text_data=json.dumps(event['payload']))

    # Signal relay — skip echoing back to the sender.
    async def chat_signal(self, event):
        if event.get('sender_channel') == self.channel_name:
            return
        await self.send(text_data=json.dumps(event['payload']))

    async def chat_pin(self, event):
        """Broadcast pin/unpin events to every open client in the room."""
        await self.send(text_data=json.dumps(event['payload']))

    # ✉️ MEMO acknowledgements. Echoed to everyone including the person
    # who acknowledged: the sender needs the running tally, and the
    # acknowledger's other tabs need to stop offering the button.
    async def chat_memo(self, event):
        await self.send(text_data=json.dumps(event['payload']))

    # ✅ READ RECEIPTS — delivered/read watermark updates.
    #
    # Unlike chat_typing we DO echo this back to the originator. The
    # sender needs their own watermark too (it drives the room's unread
    # badge), and a user with the same room open in two tabs should see
    # both tabs agree. The client ignores its own entry when colouring
    # ticks, which is the correct place for that decision.
    async def chat_receipt(self, event):
        await self.send(text_data=json.dumps(event['payload']))

    # 🔒 NEW — membership revocation.
    # RemoveRoomMemberView broadcasts this when someone is removed from a
    # room. Previously, a removed member's open socket stayed in the
    # channel-layer group and KEPT RECEIVING every new message, typing
    # event, and call signal until they disconnected on their own — a
    # real information leak. Now the matching connection detaches itself
    # from the group and closes immediately.
    async def chat_kick(self, event):
        payload = event.get('payload') or {}
        kicked_id = str(payload.get('user_id') or '')
        if kicked_id and kicked_id == str(self.user.id):
            try:
                await self.channel_layer.group_discard(self.room_group, self.channel_name)
            except Exception:
                pass
            # Tell the client why, then close.
            try:
                await self.send(text_data=json.dumps({
                    'type': 'removed_from_room',
                    'room_id': str(self.room_id),
                }))
            except Exception:
                pass
            await self.close()

    # Presence has its own event type now. Kept as a real handler so
    # nothing has to be smuggled through chat.typing ever again.
    async def chat_presence(self, event):
        payload = event.get('payload') or {}
        payload.setdefault('type', 'presence_update')
        await self.send(text_data=json.dumps(payload))

    async def chat_typing(self, event):
        """
        Forward a 'typing' signal from the channel-layer group out to this
        WebSocket client.

        🩹 PHANTOM-TYPING FIX. Three different producers push 'chat.typing'
        onto this group and they do NOT agree on a shape:

            consumers.py / typing_views.py  → fields at the TOP level
            views.py::_broadcast_typing     → fields nested under 'payload'
            views.py::_broadcast_presence_update
                                            → a PRESENCE event nested under
                                              'payload' (not typing at all)

        This handler only ever read the top level, so the last two arrived
        as a typing event whose sender_id was '' — the client compared ''
        against its own id, found them different, and rendered "Someone is
        typing…" on every presence heartbeat. Hence dots appearing when
        nobody was typing.

        We now: read either shape, hand a presence payload back as the
        presence_update it actually is, and refuse to emit a typing event
        that has no sender.

        🔒 SERVER-SIDE SELF-FILTER: never echo the typing event back to the
        person who is typing, regardless of what the client does with it.
        """
        payload = event.get('payload')
        inner = payload if isinstance(payload, dict) else event

        inner_type = str(inner.get('type') or '')

        # Presence smuggled down the typing channel — forward it correctly
        # instead of mangling it into a typing event.
        if inner_type and inner_type != 'typing' and inner_type != 'chat_typing':
            if inner_type == 'presence_update':
                await self.send(text_data=json.dumps(inner))
            return

        sender_id = str(inner.get('sender_id') or '')

        # No sender → not a real typing event. Drop it.
        if not sender_id:
            return

        if sender_id == str(self.user.id):
            return

        await self.send(text_data=json.dumps({
            'type': 'chat_typing',
            'room_id': str(inner.get('room_id') or self.room_id),
            'sender_id': sender_id,
            'sender_name': inner.get('sender_name', ''),
            'sender_initials': inner.get('sender_initials', ''),
            'sender_avatar_url': inner.get('sender_avatar_url', ''),
        }))

    # --------------------------------------------------
    # DB HELPERS
    # --------------------------------------------------

    @database_sync_to_async
    def _avatar_url(self, user):
        """Async-safe avatar lookup (staffprofile access can hit the DB)."""
        try:
            if getattr(user, 'avatar', None) and user.avatar:
                return user.avatar.url
        except Exception:
            pass
        try:
            sp = getattr(user, 'staffprofile', None)
            if sp and getattr(sp, 'profile_picture', None) and sp.profile_picture:
                return sp.profile_picture.url
        except Exception:
            pass
        return ''

    @database_sync_to_async
    def _mark_delivered(self):
        """Advance this user's delivered watermark + broadcast (best effort)."""
        try:
            from apps.messaging.models import ChatRoom
            from apps.messaging.read_receipts import mark_delivered
            room = ChatRoom.objects.filter(id=self.room_id).first()
            if room:
                mark_delivered(room, self.user)
        except Exception:
            log.exception('mark_delivered failed for room %s', self.room_id)

    @database_sync_to_async
    def _mark_read(self, up_to_message_id=None):
        """Advance this user's read watermark + broadcast (best effort)."""
        try:
            from apps.messaging.models import ChatRoom
            from apps.messaging.read_receipts import (
                mark_read, mark_read_up_to_message,
            )
            room = ChatRoom.objects.filter(id=self.room_id).first()
            if not room:
                return
            if up_to_message_id:
                mark_read_up_to_message(room, self.user, up_to_message_id)
            else:
                mark_read(room, self.user)
        except Exception:
            log.exception('mark_read failed for room %s', self.room_id)

    @database_sync_to_async
    def user_in_room(self):
        from apps.messaging.models import ChatRoom
        return ChatRoom.objects.filter(
            id=self.room_id,
            members=self.user
        ).exists()

    @database_sync_to_async
    def room_is_direct(self):
        from apps.messaging.models import ChatRoom
        return ChatRoom.objects.filter(
            id=self.room_id,
            room_type='direct',
        ).exists()

    @database_sync_to_async
    def save_and_build_payload(self, user, content, reply_to_id=None):
        """
        FULL CORRECT MESSAGE SAVE
        Uses central serializer for consistency.

        Returns a dict, NOT a bare payload-or-None:

            {'ok': True,  'payload': {...}}
            {'ok': False, 'code': '...', 'error': 'human readable reason'}

        Every early return used to be a silent None, which is precisely
        why a group message could vanish without a trace. The caller now
        has something to tell the user.
        """
        from apps.messaging.models import ChatRoom, ChatMessage, ChatRoomMember
        from apps.messaging.views import (
            _serialize_chat_message, _save_mentions, _can_post_in_room,
        )

        room = ChatRoom.objects.filter(id=self.room_id).first()
        if not room:
            log.warning('send rejected: room %s does not exist', self.room_id)
            return {'ok': False, 'code': 'no_room',
                    'error': 'This conversation no longer exists.'}

        if not room.members.filter(id=user.id).exists():
            log.warning('send rejected: user %s is not a member of room %s',
                        user.pk, self.room_id)
            return {'ok': False, 'code': 'not_member',
                    'error': 'You are no longer a member of this conversation.'}

        # 🔒 Use the same posting-permission check as the HTTP path.
        if not _can_post_in_room(user, room):
            log.warning('send rejected: user %s cannot post in room %s '
                        '(is_readonly=%s)', user.pk, self.room_id, room.is_readonly)
            return {'ok': False, 'code': 'readonly',
                    'error': 'You do not have permission to post in this room.'}

        reply_obj = None
        if reply_to_id:
            try:
                reply_obj = ChatMessage.objects.select_related('sender').get(
                    id=reply_to_id,
                    room=room,
                    is_deleted=False
                )
            except (ChatMessage.DoesNotExist, ValueError, TypeError):
                reply_obj = None

        # The write itself. This is the one step that must NOT be silently
        # swallowed: MESSAGING_ENCRYPTION_KEY being unset or wrong makes
        # encrypt_content() fail closed and raise, and the old code turned
        # that into a disappearing message.
        try:
            msg = ChatMessage.objects.create(
                room=room,
                sender=user,
                content=content,
                message_type='text',
                reply_to=reply_obj,
            )
        except Exception:
            log.exception('ChatMessage.create failed (room=%s user=%s)',
                          self.room_id, user.pk)
            return {'ok': False, 'code': 'write_failed',
                    'error': 'The server could not store your message. '
                             'It has not been sent.'}

        try:
            _save_mentions(msg)
        except Exception:
            log.exception('_save_mentions failed for message %s', msg.pk)

        try:
            room.updated_at = timezone.now()
            room.save(update_fields=['updated_at'])
        except Exception:
            log.exception('room.updated_at bump failed for room %s', self.room_id)

        try:
            ChatRoomMember.objects.filter(
                room=room,
                user=user
            ).update(last_read=timezone.now())
        except Exception:
            log.exception('last_read bump failed for room %s', self.room_id)

        # Email + push fan-out to other members. _notify_offline_members is
        # the single fan-out point for web, REST, and socket paths alike.
        try:
            from apps.messaging.views import _notify_offline_members
            _notify_offline_members(room, user, msg)
        except Exception:
            log.exception('_notify_offline_members failed for message %s', msg.pk)

        # Serialisation failing must not lose a message that IS saved —
        # report it so the client can fall back to its 3s poll.
        try:
            return {'ok': True, 'payload': _serialize_chat_message(msg, viewer=user)}
        except Exception:
            log.exception('_serialize_chat_message failed for message %s', msg.pk)
            return {'ok': False, 'code': 'serialize_failed',
                    'error': 'Message saved, but could not be displayed live. '
                             'Refresh to see it.'}

    # --------------------------------------------------
    # SAFE DISPLAY HELPERS
    # --------------------------------------------------

    def _safe_full_name(self, user):
        try:
            return user.full_name or user.get_full_name() or user.username
        except Exception:
            return str(user)

    def _safe_initials(self, user):
        try:
            if getattr(user, 'initials', None):
                return user.initials
        except Exception:
            pass

        name = self._safe_full_name(user).strip()
        if not name:
            return '?'
        parts = name.split()
        return ''.join(p[0].upper() for p in parts[:2]) or '?'
