"""
apps/files/consumers.py
────────────────────────────────────────────────────────────────────────────
Live Preview Session  —  WebSocket consumer

Group naming:
    preview_<session_token>   — shared by presenter + all viewers

Message types broadcast over the wire:
    cursor_move        {x, y}
    tool_event         {payload: {action, data}}
    chat_message       {text, sender_name, sender_id, ts, is_self}
    session_end        {}
    viewer_joined      {name}
    viewer_left        {name}
    viewer_list        {viewers: [{name}]}
    pong               {}
"""

import json
import logging
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async
from django.utils import timezone

logger = logging.getLogger(__name__)


class LivePreviewConsumer(AsyncWebsocketConsumer):

    # ── Connect ───────────────────────────────────────────────────────────────
    async def connect(self):
        self.session_token = self.scope['url_route']['kwargs']['session_token']
        self.group_name    = f'preview_{self.session_token}'
        self.user          = self.scope.get('user')

        # Cache role so we never hit the DB on every incoming message
        self._is_presenter_cached = None

        if not self.user or not self.user.is_authenticated:
            await self.close(code=4003)
            return

        # Validate — accept presenter OR anyone who was invited / added as viewer
        allowed, is_presenter = await self._check_allowed_and_role()
        if not allowed:
            await self.close(code=4004)
            return

        self._is_presenter_cached = is_presenter

        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()

        if not is_presenter:
            # Notify the group that a viewer connected (excluding self)
            await self.channel_layer.group_send(self.group_name, {
                'type':    'viewer.joined',
                'name':    self._get_name(),
                'exclude': self.channel_name,
            })
            # Send everyone an updated viewer count
            await self._broadcast_viewer_list()

    # ── Disconnect ────────────────────────────────────────────────────────────
    async def disconnect(self, close_code):
        if not hasattr(self, 'group_name'):
            return

        is_presenter = self._is_presenter_cached

        if is_presenter:
            # Mark the session ended in the DB, then notify all viewers
            await self._mark_session_ended()
            await self.channel_layer.group_send(self.group_name, {
                'type': 'session.end',
            })
        else:
            await self.channel_layer.group_send(self.group_name, {
                'type':    'viewer.left',
                'name':    self._get_name(),
                'exclude': self.channel_name,
            })
            await self._broadcast_viewer_list()

        await self.channel_layer.group_discard(self.group_name, self.channel_name)

    # ── Receive from client ───────────────────────────────────────────────────
    async def receive(self, text_data):
        try:
            msg = json.loads(text_data)
        except (json.JSONDecodeError, TypeError):
            return

        msg_type     = msg.get('type', '')
        is_presenter = self._is_presenter_cached

        # ── Ping / pong keep-alive ──
        if msg_type == 'ping':
            await self.send(text_data=json.dumps({'type': 'pong'}))
            return

        # ── Presenter-only: cursor position ──
        if msg_type == 'cursor_move' and is_presenter:
            await self.channel_layer.group_send(self.group_name, {
                'type':    'cursor.move',
                'x':       msg.get('x', 0),
                'y':       msg.get('y', 0),
                'exclude': self.channel_name,   # don't echo back to presenter
            })

        # ── Presenter-only: annotation tool events ──
        elif msg_type == 'tool_event' and is_presenter:
            await self.channel_layer.group_send(self.group_name, {
                'type':    'tool.event',
                'payload': msg.get('payload', {}),
                'exclude': self.channel_name,
            })

        # ── Chat — both presenter and viewers may send ──
        elif msg_type == 'chat_message':
            text = str(msg.get('text', '')).strip()[:1000]
            if not text:
                return
            await self.channel_layer.group_send(self.group_name, {
                'type':           'chat.message',
                'text':           text,
                'sender_name':    self._get_name(),
                'sender_id':      str(self.user.id),   # always str for safe JSON
                'ts':             timezone.now().strftime('%H:%M'),
                'sender_channel': self.channel_name,   # each recipient uses this to set is_self
            })

    # ── Group message handlers ────────────────────────────────────────────────
    # Django Channels maps 'type' to a method by replacing dots with
    # underscores: 'cursor.move' → cursor_move(), 'tool.event' → tool_event()

    async def cursor_move(self, event):
        if event.get('exclude') == self.channel_name:
            return
        await self.send(text_data=json.dumps({
            'type': 'cursor_move',
            'x':    event['x'],
            'y':    event['y'],
        }))

    async def tool_event(self, event):
        if event.get('exclude') == self.channel_name:
            return
        await self.send(text_data=json.dumps({
            'type':    'tool_event',
            'payload': event['payload'],
        }))

    async def chat_message(self, event):
        # Determine if this message was sent by this very connection
        is_self = (event.get('sender_channel') == self.channel_name)
        await self.send(text_data=json.dumps({
            'type':        'chat_message',
            'text':        event['text'],
            'sender_name': event['sender_name'],
            'sender_id':   event['sender_id'],
            'ts':          event['ts'],
            'is_self':     is_self,
        }))

    async def viewer_joined(self, event):
        if event.get('exclude') == self.channel_name:
            return
        await self.send(text_data=json.dumps({
            'type': 'viewer_joined',
            'name': event['name'],
        }))

    async def viewer_left(self, event):
        if event.get('exclude') == self.channel_name:
            return
        await self.send(text_data=json.dumps({
            'type': 'viewer_left',
            'name': event['name'],
        }))

    async def viewer_list(self, event):
        await self.send(text_data=json.dumps({
            'type':    'viewer_list',
            'viewers': event['viewers'],
        }))

    async def session_end(self, event):
        """
        Sent when the presenter disconnects or calls the end endpoint.
        We notify the client but do NOT call self.close() here — doing so
        would trigger disconnect() again and risk a double session_end loop.
        The client is responsible for closing its own WebSocket.
        """
        await self.send(text_data=json.dumps({'type': 'session_end'}))

    # ── DB helpers ────────────────────────────────────────────────────────────
    @database_sync_to_async
    def _check_allowed_and_role(self):
        """
        Returns (allowed: bool, is_presenter: bool).

        Accepts:
          - The presenter
          - Anyone in the 'invited' M2M (they accepted via the HTTP endpoint
            which adds them to 'viewers', but we also check 'invited' as a
            fallback in case the viewer WebSocket connects before the DB write
            from LivePreviewAcceptView fully commits).
          - Anyone already in the 'viewers' M2M
        """
        from apps.files.models import LivePreviewSession
        try:
            session = LivePreviewSession.objects.get(
                token=self.session_token,
                ended_at__isnull=True,
            )
            if session.presenter_id == self.user.id:
                return True, True
            is_member = (
                session.viewers.filter(id=self.user.id).exists() or
                session.invited.filter(id=self.user.id).exists()
            )
            return is_member, False
        except LivePreviewSession.DoesNotExist:
            return False, False

    @database_sync_to_async
    def _mark_session_ended(self):
        from apps.files.models import LivePreviewSession
        LivePreviewSession.objects.filter(
            token=self.session_token,
            ended_at__isnull=True,
        ).update(ended_at=timezone.now())

    @database_sync_to_async
    def _get_viewer_names(self):
        from apps.files.models import LivePreviewSession
        try:
            session = LivePreviewSession.objects.get(token=self.session_token)
            return [
                {'name': v.get_full_name() or v.username}
                for v in session.viewers.all()
            ]
        except LivePreviewSession.DoesNotExist:
            return []

    async def _broadcast_viewer_list(self):
        viewers = await self._get_viewer_names()
        await self.channel_layer.group_send(self.group_name, {
            'type':    'viewer.list',
            'viewers': viewers,
        })

    def _get_name(self):
        fn = getattr(self.user, 'get_full_name', None)
        name = fn() if callable(fn) else ''
        return name or self.user.username