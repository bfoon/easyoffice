import json
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async
from django.utils import timezone


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
    'call_upgrade_offer':   {'sdp'},
    'call_upgrade_answer':  {'sdp'},
    'call_callee_ready':    set(),
    'present_start':        {'url', 'kind', 'title', 'filename', 'page', 'total_pages'},
    'present_end':          set(),
    'present_page':         {'page'},
    'present_request_page': {'page'},
}


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

    async def disconnect(self, code):
        try:
            await self.channel_layer.group_discard(self.room_group, self.channel_name)
        except Exception:
            pass

    # --------------------------------------------------
    # RECEIVE
    # --------------------------------------------------

    async def receive(self, text_data=None, bytes_data=None):
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
                    'type': 'chat.signal',   # dedicated handler below
                    'payload': payload,
                    'sender_channel': self.channel_name,
                }
            )
            return

        # -------------------------
        # ONLY HANDLE TEXT HERE
        # (polls handled via views)
        # -------------------------
        if msg_type != 'chat_message':
            return

        message = (data.get('message') or data.get('content') or '').strip()
        if not message:
            return

        # Soft cap, mirroring the REST send path.
        if len(message) > 5000:
            message = message[:5000]

        reply_to_id = data.get('reply_to') or None

        payload = await self.save_and_build_payload(
            user,
            message,
            reply_to_id
        )

        if not payload:
            return

        await self.channel_layer.group_send(
            self.room_group,
            {
                'type': 'chat.message',
                'payload': payload,
            }
        )

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

    async def chat_typing(self, event):
        """
        Forward a 'typing' signal from the channel-layer group out to this
        WebSocket client.

        🔒 SERVER-SIDE SELF-FILTER: never echo the typing event back to the
        person who is typing. Previously this relied entirely on the client
        comparing sender_id against its own user id — an inverted or
        type-mismatched comparison in the JS (e.g. `===` vs `!==`, or a
        string UUID compared strictly against a numeric id) made the TYPER
        see their own dots while the recipient saw nothing. Filtering here
        makes the direction correct regardless of what the client does.
        """
        if str(event.get('sender_id') or '') == str(self.user.id):
            return
        await self.send(text_data=json.dumps({
            'type': 'chat_typing',
            'room_id': event.get('room_id', ''),
            'sender_id': event.get('sender_id', ''),
            'sender_name': event.get('sender_name', ''),
            'sender_initials': event.get('sender_initials', ''),
            'sender_avatar_url': event.get('sender_avatar_url', ''),
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
        Uses central serializer for consistency
        """
        from apps.messaging.models import ChatRoom, ChatMessage, ChatRoomMember
        from apps.messaging.views import (
            _serialize_chat_message, _save_mentions, _can_post_in_room,
        )

        room = ChatRoom.objects.filter(id=self.room_id).first()
        if not room:
            return None

        # 🔒 Use the same posting-permission check as the HTTP path
        # (covers is_readonly AND per-member readonly roles).
        if not _can_post_in_room(user, room):
            return None

        if not room.members.filter(id=user.id).exists():
            return None

        reply_obj = None
        if reply_to_id:
            try:
                reply_obj = ChatMessage.objects.select_related('sender').get(
                    id=reply_to_id,
                    room=room,
                    is_deleted=False
                )
            except ChatMessage.DoesNotExist:
                reply_obj = None

        msg = ChatMessage.objects.create(
            room=room,
            sender=user,
            content=content,
            message_type='text',
            reply_to=reply_obj,
        )

        try:
            _save_mentions(msg)
        except Exception:
            pass

        room.updated_at = timezone.now()
        room.save(update_fields=['updated_at'])

        ChatRoomMember.objects.filter(
            room=room,
            user=user
        ).update(last_read=timezone.now())

        # Email + push fan-out to other members. NOTE: _notify_offline_members
        # already sends Firebase push to every other member. The old code ALSO
        # had its own push loop right below, which double-notified every
        # recipient of every socket-sent message. The duplicate loop has been
        # removed — _notify_offline_members is the single fan-out point for
        # web, REST, and socket paths alike.
        try:
            from apps.messaging.views import _notify_offline_members
            _notify_offline_members(room, user, msg)
        except Exception:
            pass

        return _serialize_chat_message(msg, viewer=user)

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