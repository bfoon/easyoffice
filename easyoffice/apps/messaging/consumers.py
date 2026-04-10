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

    async def chat_reaction(self, event):
        await self.send(text_data=json.dumps(event['payload']))

    async def disconnect(self, code):
        try:
            await self.channel_layer.group_discard(self.room_group, self.channel_name)
        except Exception:
            pass

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

        message = (data.get('message') or data.get('content') or '').strip()
        if not message:
            return

        reply_to_id = data.get('reply_to') or None
        payload = await self.save_and_build_payload(user, message, reply_to_id)

        await self.channel_layer.group_send(
            self.room_group,
            {
                'type': 'chat.message',
                'payload': payload,
            }
        )

    async def chat_message(self, event):
        await self.send(text_data=json.dumps(event['payload']))

    async def chat_typing(self, event):
        await self.send(text_data=json.dumps(event['payload']))

    @database_sync_to_async
    def user_in_room(self):
        from apps.messaging.models import ChatRoom
        return ChatRoom.objects.filter(id=self.room_id, members=self.user).exists()

    @database_sync_to_async
    def save_and_build_payload(self, user, content, reply_to_id=None):
        from apps.messaging.models import ChatRoom, ChatMessage, ChatRoomMember

        room = ChatRoom.objects.get(id=self.room_id)

        reply_obj = None
        reply_sender_name = ''
        reply_content = ''

        if reply_to_id:
            try:
                reply_obj = ChatMessage.objects.select_related('sender').get(id=reply_to_id, room=room)
                reply_sender_name = self._safe_full_name(reply_obj.sender)
                reply_content = (reply_obj.content or '')[:80]
            except ChatMessage.DoesNotExist:
                reply_obj = None

        msg = ChatMessage.objects.create(
            room=room,
            sender=user,
            content=content,
            message_type='text',
            reply_to=reply_obj,
        )

        room.updated_at = timezone.now()
        room.save(update_fields=['updated_at'])

        ChatRoomMember.objects.filter(room=room, user=user).update(
            last_read=timezone.now()
        )

        return {
            'type': 'chat_message',
            'message_id': str(msg.id),
            'room_id': str(room.id),
            'sender_id': str(user.id),
            'sender_name': self._safe_full_name(user),
            'sender_initials': self._safe_initials(user),
            'message': msg.content or '',
            'content': msg.content or '',
            'message_type': msg.message_type,
            'timestamp': timezone.localtime(msg.created_at).strftime('%H:%M'),
            'created_at': timezone.localtime(msg.created_at).isoformat(),
            'reply_to_id': str(reply_obj.id) if reply_obj else None,
            'reply_to_sender': reply_sender_name,
            'reply_to_content': reply_content,
            'file_url': '',
            'file_name': '',
            'file_size': 0,
            'is_edited': False,
        }

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