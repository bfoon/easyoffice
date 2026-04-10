import json
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async


class ChatConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self.room_id = self.scope['url_route']['kwargs']['room_id']
        self.room_group = f'chat_{self.room_id}'
        await self.channel_layer.group_add(self.room_group, self.channel_name)
        await self.accept()

    async def disconnect(self, code):
        await self.channel_layer.group_discard(self.room_group, self.channel_name)

    async def receive(self, text_data=None, bytes_data=None):
        data = json.loads(text_data)
        user = self.scope['user']
        if not user.is_authenticated:
            return
        message = data.get('message', '').strip()
        if not message:
            return
        reply_to_id = data.get('reply_to') or None
        saved, reply_sender_name, reply_content = await self.save_message(user, message, reply_to_id)
        await self.channel_layer.group_send(self.room_group, {
            'type': 'chat_message',
            'message': message,
            'sender_name': user.full_name,
            'sender_id': str(user.id),
            'sender_initials': user.initials,
            'message_id': str(saved.id),
            'timestamp': saved.created_at.strftime('%H:%M'),
            'reply_to_sender': reply_sender_name,
            'reply_to_content': reply_content,
        })

    async def chat_message(self, event):
        await self.send(text_data=json.dumps(event))

    @database_sync_to_async
    def save_message(self, user, content, reply_to_id=None):
        from apps.messaging.models import ChatRoom, ChatMessage
        room = ChatRoom.objects.get(id=self.room_id)
        reply_obj = None
        reply_sender_name = None
        reply_content = None
        if reply_to_id:
            try:
                reply_obj = ChatMessage.objects.select_related('sender').get(id=reply_to_id, room=room)
                reply_sender_name = reply_obj.sender.full_name
                reply_content = (reply_obj.content or '')[:80]
            except ChatMessage.DoesNotExist:
                pass
        msg = ChatMessage.objects.create(
            room=room, sender=user, content=content,
            message_type='text', reply_to=reply_obj,
        )
        room.save()
        return msg, reply_sender_name, reply_content