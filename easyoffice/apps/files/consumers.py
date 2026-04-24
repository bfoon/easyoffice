"""
apps/files/consumers.py
────────────────────────────────────────────────────────────────────────────
Live Preview Session  —  WebSocket consumer

Group naming:
    preview_<session_token>   — shared by presenter + all viewers

Message types broadcast over the wire:
    cursor_move            {x, y, page?}
    tool_event             {payload: {action, data}}
    chat_message           {text, sender_name, sender_id, ts, is_self}
    session_end            {}
    viewer_joined          {name}
    viewer_left            {name}
    viewer_list            {viewers: [{name}]}
    presenter_disconnected {grace_seconds}
    presenter_reconnected  {}
    pdf_scroll             {scroll_pct, page}
    pong                   {}

Presenter grace-period behaviour
────────────────────────────────
When the presenter's WebSocket drops we DO NOT immediately mark the session
ended. Instead we:
    1. broadcast `presenter_disconnected` to all viewers
    2. schedule a finaliser that, after PRESENTER_GRACE_SECONDS, marks the
       session ended and broadcasts `session_end`
If the presenter reconnects before the grace window elapses we cancel the
finaliser and broadcast `presenter_reconnected`. This prevents the session
from dying on transient network drops (Wi-Fi hiccups, proxy idle timeouts,
mobile suspensions, etc.) while still cleaning up sessions whose presenter
has actually left.

NOTE on multi-worker deployments:
The pending finalisers are held in a process-local dict. If you run multiple
ASGI workers the presenter may reconnect to a different worker than the one
holding the pending task. The reconnecting consumer will still clear
`ended_at` back to NULL (see `_reclaim_session`), and the finaliser itself
re-checks the DB before acting, so the worst case is that the original task
fires a harmless broadcast after the presenter has been re-seen by another
worker. If this matters for your deployment, move the pending-task registry
into Redis / the channel layer.
"""

import asyncio
import json
import logging
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async
from django.utils import timezone

logger = logging.getLogger(__name__)

# Seconds to wait before actually ending a session after the presenter
# disconnects. Must match the number the client shows in its "Presenter
# offline — reconnecting…" UI.
PRESENTER_GRACE_SECONDS = 15

# Process-local registry of pending "end session" tasks, keyed by session_token.
# If the presenter reconnects we cancel the task here.
_PENDING_END_TASKS: "dict[str, asyncio.Task]" = {}


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

        # Validate — accept presenter OR anyone who was invited / added as viewer.
        # Presenters are accepted even if the session is currently in a
        # grace-period "ended" state, as this is exactly the case we want
        # to recover from.
        allowed, is_presenter = await self._check_allowed_and_role()
        if not allowed:
            await self.close(code=4004)
            return

        self._is_presenter_cached = is_presenter

        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()

        if is_presenter:
            # Presenter reconnected during grace window → cancel the pending
            # end-session task and let everyone know we're back.
            pending = _PENDING_END_TASKS.pop(self.session_token, None)
            if pending and not pending.done():
                pending.cancel()
                # Clear any grace-period ended_at that a previous disconnect set.
                await self._reclaim_session()
                await self.channel_layer.group_send(self.group_name, {
                    'type': 'presenter.reconnected',
                })
        else:
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
            # Don't end the session immediately — give the presenter a grace
            # window in which to reconnect (handles Wi-Fi blips, mobile
            # suspensions, idle-proxy kills, etc.).
            await self.channel_layer.group_send(self.group_name, {
                'type':          'presenter.disconnected',
                'grace_seconds': PRESENTER_GRACE_SECONDS,
            })

            # Cancel any previous pending task for this session before
            # starting a new one (shouldn't happen, but defensive).
            prev = _PENDING_END_TASKS.pop(self.session_token, None)
            if prev and not prev.done():
                prev.cancel()

            # Schedule the real end-session after the grace period.
            loop = asyncio.get_event_loop()
            task = loop.create_task(
                self._finalise_session_after_grace(self.session_token)
            )
            _PENDING_END_TASKS[self.session_token] = task
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
            # Cursor can be either:
            #   • page-relative    →  {x, y, page}   (inside a PDF page wrap)
            #   • surface-relative →  {x, y}         (non-PDF)
            await self.channel_layer.group_send(self.group_name, {
                'type':    'cursor.move',
                'x':       msg.get('x', 0),
                'y':       msg.get('y', 0),
                'page':    msg.get('page'),   # None for non-PDF
                'exclude': self.channel_name,
            })

        # ── Presenter-only: annotation tool events ──
        elif msg_type == 'tool_event' and is_presenter:
            await self.channel_layer.group_send(self.group_name, {
                'type':    'tool.event',
                'payload': msg.get('payload', {}),
                'exclude': self.channel_name,
            })

        # ── Presenter-only: PDF scroll / page change ──
        elif msg_type == 'pdf_scroll' and is_presenter:
            await self.channel_layer.group_send(self.group_name, {
                'type':       'pdf.scroll',
                'scroll_pct': msg.get('scroll_pct', 0),
                'page':       msg.get('page', 1),
                'exclude':    self.channel_name,
            })

        # ── Presenter: respond to a viewer's sync request ──
        elif msg_type == 'sync_state' and is_presenter:
            requester = msg.get('requester_channel')
            if requester:
                await self.channel_layer.send(requester, {
                    'type':       'pdf.scroll',
                    'scroll_pct': msg.get('scroll_pct', 0),
                    'page':       msg.get('page', 1),
                    'exclude':    '',
                })

        # ── Viewer: request current state from presenter ──
        elif msg_type == 'request_sync' and not is_presenter:
            await self.channel_layer.group_send(self.group_name, {
                'type':              'sync.request',
                'requester_channel': self.channel_name,
            })

        elif msg_type == 'chat_message':
            text = str(msg.get('text', '')).strip()[:1000]
            if not text:
                return
            await self.channel_layer.group_send(self.group_name, {
                'type':           'chat.message',
                'text':           text,
                'sender_name':    self._get_name(),
                'sender_id':      str(self.user.id),
                'ts':             timezone.now().strftime('%H:%M'),
                'sender_channel': self.channel_name,
            })

    # ── Group message handlers ────────────────────────────────────────────────

    async def cursor_move(self, event):
        if event.get('exclude') == self.channel_name:
            return
        out = {
            'type': 'cursor_move',
            'x':    event['x'],
            'y':    event['y'],
        }
        if event.get('page') is not None:
            out['page'] = event['page']
        await self.send(text_data=json.dumps(out))

    async def tool_event(self, event):
        if event.get('exclude') == self.channel_name:
            return
        await self.send(text_data=json.dumps({
            'type':    'tool_event',
            'payload': event['payload'],
        }))

    async def chat_message(self, event):
        is_self = (event.get('sender_channel') == self.channel_name)
        await self.send(text_data=json.dumps({
            'type':        'chat_message',
            'text':        event['text'],
            'sender_name': event['sender_name'],
            'sender_id':   event['sender_id'],
            'ts':          event['ts'],
            'is_self':     is_self,
        }))

    async def pdf_scroll(self, event):
        if event.get('exclude') == self.channel_name:
            return
        await self.send(text_data=json.dumps({
            'type':       'pdf_scroll',
            'scroll_pct': event['scroll_pct'],
            'page':       event['page'],
        }))

    async def sync_request(self, event):
        """Only the presenter should respond to this."""
        if not self._is_presenter_cached:
            return
        await self.send(text_data=json.dumps({
            'type':              'sync_request',
            'requester_channel': event['requester_channel'],
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
        Sent when the presenter disconnects permanently (grace window
        elapsed) or calls the end endpoint. We notify the client but do
        NOT call self.close() here — the client is responsible for closing
        its own WS.
        """
        await self.send(text_data=json.dumps({'type': 'session_end'}))

    async def presenter_disconnected(self, event):
        """Tell viewers the presenter dropped — we're in the grace window."""
        if self._is_presenter_cached:
            return   # don't tell the presenter about themselves
        await self.send(text_data=json.dumps({
            'type':          'presenter_disconnected',
            'grace_seconds': event.get('grace_seconds', PRESENTER_GRACE_SECONDS),
        }))

    async def presenter_reconnected(self, event):
        """Tell viewers the presenter is back."""
        if self._is_presenter_cached:
            return
        await self.send(text_data=json.dumps({
            'type': 'presenter_reconnected',
        }))

    # ── Grace-window finaliser ────────────────────────────────────────────────
    async def _finalise_session_after_grace(self, session_token):
        """
        Sleep for PRESENTER_GRACE_SECONDS, then mark the session ended and
        notify all viewers. Cancelled when the presenter reconnects in time.
        """
        try:
            await asyncio.sleep(PRESENTER_GRACE_SECONDS)

            # Presenter didn't come back. Re-check in the DB in case the
            # presenter already called the explicit end endpoint.
            still_active = await self._is_session_active()
            if not still_active:
                return

            await self._mark_session_ended()
            try:
                from channels.layers import get_channel_layer
                layer = get_channel_layer()
                if layer:
                    await layer.group_send(
                        f'preview_{session_token}',
                        {'type': 'session_end'},
                    )
            except Exception as exc:
                logger.warning('grace finaliser broadcast failed: %s', exc)
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception('live-preview grace finaliser crashed')
        finally:
            _PENDING_END_TASKS.pop(session_token, None)

    # ── DB helpers ────────────────────────────────────────────────────────────
    @database_sync_to_async
    def _check_allowed_and_role(self):
        """
        Returns (allowed: bool, is_presenter: bool).

        The presenter is allowed to reconnect even during the grace window
        (when ended_at was set by a previous disconnect but the finaliser
        hasn't fired yet). Viewers are also allowed to reconnect during
        the grace window — if the session ultimately ends when grace
        expires the server will broadcast session_end and they'll be
        torn down then.
        """
        from apps.files.models import LivePreviewSession
        try:
            in_grace = self.session_token in _PENDING_END_TASKS

            qs = LivePreviewSession.objects.filter(token=self.session_token)
            if not in_grace:
                qs = qs.filter(ended_at__isnull=True)
            session = qs.first()
            if not session:
                return False, False

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
    def _is_session_active(self):
        from apps.files.models import LivePreviewSession
        return LivePreviewSession.objects.filter(
            token=self.session_token, ended_at__isnull=True,
        ).exists()

    @database_sync_to_async
    def _reclaim_session(self):
        """
        Clear a grace-period ended_at so the session goes back to being
        active for new viewer connections.
        """
        from apps.files.models import LivePreviewSession
        LivePreviewSession.objects.filter(
            token=self.session_token,
        ).update(ended_at=None)

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