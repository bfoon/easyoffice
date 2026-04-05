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
        saved = await self.save_message(user, message)
        await self.channel_layer.group_send(self.room_group, {
            'type': 'chat_message',
            'message': message,
            'sender_name': user.full_name,
            'sender_id': str(user.id),
            'sender_initials': user.initials,
            'message_id': str(saved.id),
            'timestamp': saved.created_at.strftime('%H:%M'),
        })

    async def chat_message(self, event):
        await self.send(text_data=json.dumps(event))

    @database_sync_to_async
    def save_message(self, user, content):
        from apps.messaging.models import ChatRoom, ChatMessage
        room = ChatRoom.objects.get(id=self.room_id)
        return ChatMessage.objects.create(
            room=room, sender=user, content=content, message_type='text'
        )
