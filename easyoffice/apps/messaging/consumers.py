import json
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async
from django.utils import timezone


class ChatConsumer(AsyncWebsocketConsumer):

    async def connect(self):
        self.room_id = self.scope['url_route']['kwargs']['room_id']
        self.room_group = f'chat_{self.room_id}'
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
            await self.channel_layer.group_send(
                self.room_group,
                {
                    'type': 'chat.typing',
                    'payload': {
                        'type': 'typing',
                        'room_id': str(self.room_id),
                        'sender_id': str(user.id),
                        'sender_name': self._safe_full_name(user),
                    }
                }
            )
            return

        # -------------------------
        # 🆕 VOICE-CALL SIGNALING
        # -------------------------
        # WebRTC needs to exchange SDP offers/answers and ICE candidates
        # between the two peers. We relay these blobs as-is through the
        # room group — the server never inspects or stores them, and the
        # actual audio stream is peer-to-peer (not through the server).
        #
        # Supported signaling types (all 1-on-1):
        #   call_offer      { sdp }
        #   call_answer     { sdp }
        #   ice_candidate   { candidate }
        #   call_hangup     {}
        #   call_decline    {}
        #   call_cancel     {}          (caller aborts before answer)
        if msg_type in (
            'call_offer', 'call_answer', 'ice_candidate',
            'call_hangup', 'call_decline', 'call_cancel',
            # 🆕 Upgrade an in-progress voice call to video (full
            # renegotiation — new offer/answer exchange, same peers).
            'call_upgrade_offer', 'call_upgrade_answer',
            # 🩹 Callee-popup announces itself when it joins the room
            # group. The caller-popup responds by re-sending its
            # call_offer so popups that opened AFTER the original offer
            # broadcast still receive it.
            'call_callee_ready',
            # 🆕 Document-presentation sync. One presenter at a time;
            # viewers can request a specific page jump which the
            # presenter may honour.
            'present_start', 'present_end', 'present_page',
            'present_request_page',
        ):
            # Must be a DM room — enforce 1-on-1 scope.
            is_direct = await self.room_is_direct()
            if not is_direct:
                return

            payload = dict(data)
            payload['sender_id']   = str(user.id)
            payload['sender_name'] = self._safe_full_name(user)
            payload['room_id']     = str(self.room_id)

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

    async def chat_typing(self, event):
        await self.send(text_data=json.dumps(event['payload']))

    async def chat_reaction(self, event):
        await self.send(text_data=json.dumps(event['payload']))

    async def chat_poll(self, event):
        await self.send(text_data=json.dumps(event['payload']))

    async def chat_edit(self, event):
        await self.send(text_data=json.dumps(event['payload']))

    # 🆕 Signal relay — skip echoing back to the sender.
    async def chat_signal(self, event):
        if event.get('sender_channel') == self.channel_name:
            return
        await self.send(text_data=json.dumps(event['payload']))

    # --------------------------------------------------
    # DB HELPERS
    # --------------------------------------------------

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
        from apps.messaging.views import _serialize_chat_message, _save_mentions

        room = ChatRoom.objects.filter(id=self.room_id).first()
        if not room:
            return None

        if room.is_readonly:
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