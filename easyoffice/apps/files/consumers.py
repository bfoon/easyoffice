"""
apps/files/consumers.py
────────────────────────────────────────────────────────────────────────────
Live Preview Session  —  WebSocket consumer

Group naming:
    preview_<session_token>      — both presenter + viewers

Message types sent over the wire (type field):
    cursor_move              {x, y}                              presenter → viewers
    tool_event               {payload}                           presenter → viewers
    chat_message             {text, sender_name, sender_id, ts}  any ↔ all
    viewer_joined            {user_id, name}                     server → everyone else
    viewer_left              {user_id, name}                     server → everyone else
    viewer_list              {viewers: [{id, name}, ...]}        server → everyone
    presenter_disconnected   {grace_seconds}                     server → viewers
    presenter_reconnected    {}                                  server → viewers
    session_end              {}                                  server → all
    ping / pong              {}                                  keep-alive
    error                    {code, message}                     server → sender
────────────────────────────────────────────────────────────────────────────
Notes
-----
•  The presenter may reconnect within PRESENTER_GRACE_SECONDS without the
   session being torn down — handy for brief network blips.  The grace is
   tracked in-process; if the presenter lands on a different Channels worker
   during the blip, the session will still end.  For a single-worker deploy
   this is transparent.

•  Chat messages are lightly rate-limited (CHAT_RATE_MAX msgs per
   CHAT_RATE_WINDOW seconds per user) to make casual spam unpleasant.

•  Viewers are read-only: they may send chat but cannot broadcast
   cursor_move or tool_event.  The front-end also hides the drawing
   toolbar for viewers, so the check here is defense-in-depth.
"""

import asyncio
import json
import time
from collections import defaultdict, deque

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer
from django.utils import timezone


# ── Tunables ─────────────────────────────────────────────────────────────────
PRESENTER_GRACE_SECONDS = 10     # how long to wait for presenter reconnect
CHAT_RATE_MAX           = 5      # messages …
CHAT_RATE_WINDOW        = 3.0    # … per this many seconds per user
CHAT_MAX_LEN            = 1000

# In-process map: session_token → asyncio.Task running the grace-period kill.
# Module-level so it survives across consumer instances in the same process.
_PENDING_ENDS: dict = {}


class LivePreviewConsumer(AsyncWebsocketConsumer):

    # ── Connect ──────────────────────────────────────────────────────────────
    async def connect(self):
        self.session_token = self.scope['url_route']['kwargs']['session_token']
        self.group_name    = f'preview_{self.session_token}'
        self.user          = self.scope.get('user')

        if not self.user or not self.user.is_authenticated:
            await self.close(code=4003)
            return

        # Resolve role AND membership in a single DB hit, auto-joining as a
        # viewer if the caller was merely invited (covers the accept → WS race).
        role = await self._resolve_role_and_join()
        if role is None:
            await self.close(code=4004)
            return

        self.role = role  # 'presenter' | 'viewer'
        self.display_name = getattr(
            self.user, 'get_full_name', lambda: self.user.username
        )() or self.user.username

        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()

        if self.role == 'presenter':
            # Cancel any pending session-end that was waiting for reconnect.
            task = _PENDING_ENDS.pop(self.session_token, None)
            if task and not task.done():
                task.cancel()
                # Tell viewers the presenter is back.
                await self.channel_layer.group_send(self.group_name, {
                    'type': 'presenter_reconnected',
                })
        else:
            # Announce to everyone else that a viewer joined.
            await self.channel_layer.group_send(self.group_name, {
                'type':    'viewer_joined',
                'user_id': self.user.id,
                'name':    self.display_name,
                'exclude': self.channel_name,
            })

        # Always refresh the roster after anyone joins.
        await self._broadcast_roster()

    # ── Disconnect ───────────────────────────────────────────────────────────
    async def disconnect(self, close_code):
        if not hasattr(self, 'group_name'):
            return

        await self.channel_layer.group_discard(self.group_name, self.channel_name)

        if getattr(self, 'role', None) == 'presenter':
            # Start (or replace) the grace-period killer task.
            prev = _PENDING_ENDS.pop(self.session_token, None)
            if prev and not prev.done():
                prev.cancel()

            # Notify viewers immediately — they can show "presenter offline…"
            await self.channel_layer.group_send(self.group_name, {
                'type':           'presenter_disconnected',
                'grace_seconds':  PRESENTER_GRACE_SECONDS,
            })

            _PENDING_ENDS[self.session_token] = asyncio.create_task(
                self._end_session_after_grace()
            )
        elif getattr(self, 'role', None) == 'viewer':
            await self.channel_layer.group_send(self.group_name, {
                'type':    'viewer_left',
                'user_id': self.user.id,
                'name':    self.display_name,
                'exclude': self.channel_name,
            })
            await self._broadcast_roster()

    async def _end_session_after_grace(self):
        """Wait the grace window, then end the session if the presenter
        hasn't reconnected.  Cancelled by connect() if they do."""
        try:
            await asyncio.sleep(PRESENTER_GRACE_SECONDS)
        except asyncio.CancelledError:
            return

        # Presenter didn't come back — kill it.
        await self._mark_session_ended()
        await self.channel_layer.group_send(self.group_name, {
            'type': 'session_end',
        })
        _PENDING_ENDS.pop(self.session_token, None)

    # ── Receive from client ───────────────────────────────────────────────────
    async def receive(self, text_data=None, bytes_data=None):
        if not text_data:
            return
        try:
            msg = json.loads(text_data)
        except (json.JSONDecodeError, TypeError, ValueError):
            return

        msg_type = msg.get('type', '')

        if msg_type == 'ping':
            await self.send(text_data=json.dumps({'type': 'pong'}))
            return

        is_presenter = (getattr(self, 'role', None) == 'presenter')

        if msg_type == 'cursor_move':
            if not is_presenter:
                return
            try:
                x = float(msg.get('x', 0))
                y = float(msg.get('y', 0))
            except (TypeError, ValueError):
                return
            # Clamp to [0, 100] so a bad client can't move the cursor off-canvas.
            x = max(0.0, min(100.0, x))
            y = max(0.0, min(100.0, y))
            await self.channel_layer.group_send(self.group_name, {
                'type':    'cursor_move',
                'x':       x,
                'y':       y,
                'exclude': self.channel_name,
            })

        elif msg_type == 'tool_event':
            if not is_presenter:
                return
            payload = msg.get('payload') or {}
            if not isinstance(payload, dict):
                return
            await self.channel_layer.group_send(self.group_name, {
                'type':    'tool_event',
                'payload': payload,
                'exclude': self.channel_name,
            })

        elif msg_type == 'chat_message':
            if not self._chat_allow():
                await self.send(text_data=json.dumps({
                    'type':    'error',
                    'code':    'rate_limited',
                    'message': 'Slow down — too many messages.',
                }))
                return
            text = str(msg.get('text', ''))[:CHAT_MAX_LEN].strip()
            if not text:
                return
            ts = timezone.localtime().strftime('%H:%M')
            await self.channel_layer.group_send(self.group_name, {
                'type':             'chat_message',
                'text':             text,
                'sender_name':      self.display_name,
                'sender_id':        self.user.id,
                'ts':               ts,
                'is_self_channel':  self.channel_name,
            })

    # ── Group message handlers ────────────────────────────────────────────────
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
        is_self = (event.get('is_self_channel') == self.channel_name)
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
            'type':    'viewer_joined',
            'user_id': event['user_id'],
            'name':    event['name'],
        }))

    async def viewer_left(self, event):
        if event.get('exclude') == self.channel_name:
            return
        await self.send(text_data=json.dumps({
            'type':    'viewer_left',
            'user_id': event['user_id'],
            'name':    event['name'],
        }))

    async def viewer_list(self, event):
        await self.send(text_data=json.dumps({
            'type':    'viewer_list',
            'viewers': event['viewers'],
        }))

    async def presenter_disconnected(self, event):
        # Presenter doesn't need to hear about their own disconnect.
        if getattr(self, 'role', None) == 'presenter':
            return
        await self.send(text_data=json.dumps({
            'type':          'presenter_disconnected',
            'grace_seconds': event['grace_seconds'],
        }))

    async def presenter_reconnected(self, event):
        if getattr(self, 'role', None) == 'presenter':
            return
        await self.send(text_data=json.dumps({
            'type': 'presenter_reconnected',
        }))

    async def session_end(self, event):
        await self.send(text_data=json.dumps({'type': 'session_end'}))
        await self.close()

    # ── Helpers ───────────────────────────────────────────────────────────────
    async def _broadcast_roster(self):
        """Fetch the current viewers list and push to everyone in the group."""
        viewers = await self._get_roster()
        await self.channel_layer.group_send(self.group_name, {
            'type':    'viewer_list',
            'viewers': viewers,
        })

    # ── Chat rate limiting (per-user, in-process) ────────────────────────────
    # Module-level so it survives per user even across consumer instances in
    # the same worker.  For a multi-worker setup this is best-effort.
    _CHAT_LOG: dict = defaultdict(lambda: deque(maxlen=CHAT_RATE_MAX + 4))

    def _chat_allow(self) -> bool:
        now   = time.monotonic()
        key   = (self.session_token, self.user.id)
        log   = LivePreviewConsumer._CHAT_LOG[key]
        while log and (now - log[0]) > CHAT_RATE_WINDOW:
            log.popleft()
        if len(log) >= CHAT_RATE_MAX:
            return False
        log.append(now)
        return True

    # ── DB helpers ────────────────────────────────────────────────────────────
    @database_sync_to_async
    def _resolve_role_and_join(self):
        """Return 'presenter', 'viewer', or None (not allowed)."""
        from apps.files.models import LivePreviewSession
        try:
            session = LivePreviewSession.objects.get(
                token=self.session_token,
                ended_at__isnull=True,
            )
        except LivePreviewSession.DoesNotExist:
            return None

        if session.presenter_id == self.user.id:
            return 'presenter'
        if session.viewers.filter(id=self.user.id).exists():
            return 'viewer'
        # Auto-promote from "invited" to "viewer" on WS connect — this closes
        # the accept-POST → WS-connect race the old code suffered from.
        if session.invited.filter(id=self.user.id).exists():
            session.viewers.add(self.user)
            return 'viewer'
        return None

    @database_sync_to_async
    def _get_roster(self):
        from apps.files.models import LivePreviewSession
        try:
            session = LivePreviewSession.objects.get(token=self.session_token)
        except LivePreviewSession.DoesNotExist:
            return []
        return [
            {
                'id':   v.id,
                'name': (v.get_full_name() or v.username),
            }
            for v in session.viewers.all()
        ]

    @database_sync_to_async
    def _mark_session_ended(self):
        from apps.files.models import LivePreviewSession
        LivePreviewSession.objects.filter(
            token=self.session_token,
            ended_at__isnull=True,
        ).update(ended_at=timezone.now())