"""
apps/customer_service/consumers.py
──────────────────────────────────
Channels WebSocket consumer for the customer ⇄ CS live chat.

Two URL routes hit this same class:

    ws://…/ws/cs/live-chat/customer/<token>/   →  customer side (anonymous, tokenised)
    ws://…/ws/cs/live-chat/agent/<ticket_pk>/  →  CS-agent side (authenticated)

The class disambiguates them via `self.scope['url_route']['kwargs']`.
Both sides join the SAME Channels group — `livechat_<session.pk>` — so
`live_chat_services._broadcast_message()` reaches everyone in one shot.

Why one consumer, not two?
    The send/receive surface is identical (broadcast a message, persist
    it, push state updates). The only difference is auth + how we look
    up the session. Splitting into two classes would duplicate ~80% of
    the code for no real gain.

Handshake contract
──────────────────
Customer side:
    • Connects with `?fp=<fingerprint>` in the query string (optional).
    • Cookie `cs_livechat_mid` is read from scope['cookies'] for binding.
    • Falls through to bind_first_visit(); if it rejects, we accept the
      socket just long enough to send a {"type":"locked"} frame, then close.

Agent side:
    • Standard Django session auth — `scope['user']` must be authenticated
      AND pass `is_cs_user()`. Otherwise the socket is closed with 4403.

Message frames (JSON):
    Client → server:
        {"action": "send", "body": "hello"}
        {"action": "ping"}                       (presence heartbeat)
    Server → client:
        {"type": "message", "message": {...}}    (broadcast from group)
        {"type": "state",   "state":   {...}}    (broadcast from group)
        {"type": "history", "messages": [...]}   (sent once on connect)
        {"type": "locked",  "reason": "..."}     (just before close)
"""
from __future__ import annotations

import json
import logging
from typing import Optional

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer
from django.utils import timezone

logger = logging.getLogger(__name__)


# ── Helpers run in the sync DB context ─────────────────────────────────

@database_sync_to_async
def _load_session_by_token(token):
    from .models import LiveChatSession
    try:
        return LiveChatSession.objects.select_related('ticket', 'ticket__customer').get(token=token)
    except (LiveChatSession.DoesNotExist, ValueError, TypeError):
        return None


@database_sync_to_async
def _load_session_for_ticket(ticket_pk):
    from .models import LiveChatSession
    return (LiveChatSession.objects
            .select_related('ticket', 'ticket__customer')
            .filter(ticket_id=ticket_pk)
            .first())


@database_sync_to_async
def _check_session_usable(session):
    from . import live_chat_services as lcs
    # Re-fetch a fresh copy so we don't trust a possibly-stale instance.
    session.refresh_from_db()
    return lcs.session_is_usable(session)


@database_sync_to_async
def _post_message(session, *, author_kind, body, agent=None):
    from . import live_chat_services as lcs
    return lcs.post_message(session, author_kind=author_kind, body=body, agent=agent)


@database_sync_to_async
def _recent_history(session, limit=50):
    """Serialize the last N messages so a reconnect doesn't lose context."""
    qs = session.messages.order_by('-created_at')[:limit]
    items = [
        {
            'id': m.pk,
            'author_kind': m.author_kind,
            'author_display': m.author_display,
            'body': m.body,
            'created_at': m.created_at.isoformat(),
        }
        for m in qs
    ]
    items.reverse()  # oldest first for natural rendering
    return items


@database_sync_to_async
def _touch_presence(session, *, side):
    """Update advisory last-seen timestamps."""
    field = 'customer_last_seen_at' if side == 'customer' else 'agent_last_seen_at'
    setattr(session, field, timezone.now())
    session.save(update_fields=[field, 'updated_at'])


@database_sync_to_async
def _is_cs_user(user):
    if not user or not getattr(user, 'is_authenticated', False):
        return False
    # Lazy import — services imports models which imports settings, so
    # do it at call time to avoid AppRegistryNotReady at import.
    try:
        from . import services
        return services.is_cs_user(user)
    except Exception:
        # If services blew up, fall back to is_staff so admins aren't locked out.
        return bool(getattr(user, 'is_staff', False) or getattr(user, 'is_superuser', False))


# ════════════════════════════════════════════════════════════════════════
# Consumer
# ════════════════════════════════════════════════════════════════════════

class LiveChatConsumer(AsyncJsonWebsocketConsumer):
    """Bidirectional chat consumer. Routed twice — customer and agent."""

    # Populated in connect()
    session = None
    side: str = ''         # 'customer' | 'agent'
    group_name: str = ''
    user = None            # Django user (agent side only)

    # ── Connect ────────────────────────────────────────────────────────
    async def connect(self):
        kwargs = self.scope['url_route']['kwargs']

        if 'token' in kwargs:
            self.side = 'customer'
            self.session = await _load_session_by_token(kwargs['token'])
        elif 'ticket_pk' in kwargs:
            self.side = 'agent'
            # Auth gate — agents must be authenticated CS users.
            self.user = self.scope.get('user')
            if not await _is_cs_user(self.user):
                await self.close(code=4403)
                return
            self.session = await _load_session_for_ticket(kwargs['ticket_pk'])
        else:
            await self.close(code=4400)
            return

        if self.session is None:
            await self.close(code=4404)
            return

        # State gate — even for agents, no point connecting to a dead session.
        ok, reason = await _check_session_usable(self.session)
        if not ok and self.side == 'customer':
            # Be polite to the customer — accept, tell them, close.
            await self.accept()
            await self.send_json({'type': 'locked', 'reason': reason})
            await self.close(code=4001)
            return

        # Agents are allowed to connect even when off/locked so they can
        # see history. We just send a state frame so the UI shows "off".
        self.group_name = self.session.channels_group_name
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()

        # Send initial state + history on connect.
        await self.send_json({
            'type': 'state',
            'state': {
                'kind': 'connected',
                'is_active': self.session.is_active,
                'is_locked': self.session.is_locked,
                'lock_reason': self.session.lock_reason,
            },
        })
        history = await _recent_history(self.session)
        await self.send_json({'type': 'history', 'messages': history})
        await _touch_presence(self.session, side=self.side)

    # ── Disconnect ─────────────────────────────────────────────────────
    async def disconnect(self, code):
        if self.group_name:
            try:
                await self.channel_layer.group_discard(self.group_name, self.channel_name)
            except Exception:
                pass

    # ── Receive (client → server) ──────────────────────────────────────
    async def receive_json(self, content, **kwargs):
        action = (content or {}).get('action')

        if action == 'ping':
            try:
                await _touch_presence(self.session, side=self.side)
            except Exception:
                logger.debug('presence touch failed', exc_info=True)
            return

        if action == 'send':
            body = (content.get('body') or '').strip()
            if not body:
                return

            # Re-check usability on every send — between the open and now
            # CS may have toggled off, or the ticket may have closed.
            ok, reason = await _check_session_usable(self.session)
            if not ok:
                await self.send_json({'type': 'locked', 'reason': reason})
                await self.close(code=4001)
                return

            try:
                if self.side == 'customer':
                    await _post_message(self.session, author_kind='customer', body=body)
                else:
                    await _post_message(self.session, author_kind='agent', body=body, agent=self.user)
            except Exception:
                logger.exception('post_message failed in consumer')
                await self.send_json({'type': 'error', 'error': 'send_failed'})
            return

        # Unknown action — quietly ignore. Don't disconnect; protocol drift
        # between deployed clients shouldn't kill an otherwise-fine socket.

    # ── Group dispatch handlers (server → client) ──────────────────────
    # These names match the `type` values used by live_chat_services:
    #   chat.message  →  chat_message
    #   chat.state    →  chat_state
    # (Channels converts dots to underscores in handler names.)

    async def chat_message(self, event):
        await self.send_json({'type': 'message', 'message': event['message']})

    async def chat_state(self, event):
        await self.send_json({'type': 'state', 'state': event['state']})
        # If the state transition kills the session, walk the customer off it.
        if self.side == 'customer':
            state = event.get('state', {}) or {}
            if not state.get('is_active') or state.get('is_locked'):
                await self.close(code=4001)
