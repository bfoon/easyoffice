"""apps/customer_service/consumers.py
Channels consumer for ticket live chat.
"""
from __future__ import annotations

from urllib.parse import parse_qs

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer


class LiveChatConsumer(AsyncJsonWebsocketConsumer):
    """Shared consumer for customer token links and authenticated agents."""

    async def connect(self):
        self.mode = None
        self.session_id = None
        self.group_name = None
        self.agent_user_id = None

        kwargs = self.scope.get('url_route', {}).get('kwargs', {})
        token = kwargs.get('token')
        ticket_pk = kwargs.get('ticket_pk')

        if token:
            result = await self._resolve_customer_session(str(token))
        else:
            result = await self._resolve_agent_session(ticket_pk)

        if not result.get('ok'):
            await self.close(code=4403)
            return

        self.mode = result['mode']
        self.session_id = result['session_id']
        self.group_name = result['group_name']
        self.agent_user_id = result.get('agent_user_id')

        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()

        await self.send_json({
            'type': 'state',
            'state': result['state'],
        })
        await self.send_json({
            'type': 'history',
            'messages': await self._recent_messages(self.session_id),
        })

    async def disconnect(self, close_code):
        if self.group_name:
            await self.channel_layer.group_discard(self.group_name, self.channel_name)

    async def receive_json(self, content, **kwargs):
        msg_type = (content.get('type') or content.get('action') or 'message').strip().lower()
        if msg_type in {'ping', 'heartbeat'}:
            await self.send_json({'type': 'pong'})
            return

        if msg_type != 'message':
            await self.send_json({'type': 'error', 'message': 'Unknown action.'})
            return

        body = (content.get('body') or '').strip()
        if not body:
            return

        result = await self._post_message(self.session_id, self.mode, body, self.agent_user_id)
        if not result.get('ok'):
            await self.send_json({'type': 'error', 'message': result.get('error', 'Message failed.')})

    async def chat_message(self, event):
        await self.send_json({'type': 'message', 'message': event.get('message', {})})

    async def chat_state(self, event):
        await self.send_json({'type': 'state', 'state': event.get('state', {})})

    def _cookie_value(self, name: str) -> str:
        headers = dict(self.scope.get('headers') or [])
        raw_cookie = headers.get(b'cookie', b'').decode('latin1')
        for part in raw_cookie.split(';'):
            if '=' not in part:
                continue
            k, v = part.strip().split('=', 1)
            if k == name:
                return v
        return ''

    @database_sync_to_async
    def _resolve_customer_session(self, token: str) -> dict:
        from .models import LiveChatSession
        from . import live_chat_services as lcs

        session = (
            LiveChatSession.objects
            .select_related('ticket', 'ticket__customer')
            .filter(token=token)
            .first()
        )
        if not session:
            return {'ok': False, 'reason': 'unknown'}

        ok, reason = lcs.session_is_usable(session)
        if not ok:
            return {'ok': False, 'reason': reason}

        cookie = self._cookie_value(lcs.MACHINE_COOKIE_NAME)
        # If we have a stored binding AND the handshake carried a cookie,
        # it must match. A *missing* cookie on the WS handshake is NOT
        # treated as a different device — the page GET already validated
        # the device and bound it; the socket simply may not echo the
        # cookie depending on the client. Posting is still gated by
        # session_is_usable() inside post_message().
        if session.machine_cookie and cookie and cookie != session.machine_cookie:
            return {'ok': False, 'reason': 'device_not_bound'}

        return {
            'ok': True,
            'mode': 'customer',
            'session_id': session.pk,
            'group_name': session.channels_group_name,
            'state': self._state_payload(session),
        }

    @database_sync_to_async
    def _resolve_agent_session(self, ticket_pk) -> dict:
        from .models import ServiceTicket
        from .permissions import can_use_customer_service
        from . import live_chat_services as lcs

        user = self.scope.get('user')
        if not user or not user.is_authenticated or not can_use_customer_service(user):
            return {'ok': False, 'reason': 'unauthenticated'}

        ticket = ServiceTicket.objects.select_related('customer').filter(pk=ticket_pk).first()
        if not ticket:
            return {'ok': False, 'reason': 'ticket_not_found'}

        session = lcs.get_or_create_session(ticket)
        return {
            'ok': True,
            'mode': 'agent',
            'agent_user_id': user.pk,
            'session_id': session.pk,
            'group_name': session.channels_group_name,
            'state': self._state_payload(session),
        }

    @database_sync_to_async
    def _recent_messages(self, session_id) -> list[dict]:
        from .models import LiveChatMessage
        qs = (
            LiveChatMessage.objects
            .filter(session_id=session_id)
            .select_related('agent', 'session__ticket', 'session__ticket__customer')
            .order_by('-created_at')[:80]
        )
        return [
            {
                'id': m.pk,
                'author_kind': m.author_kind,
                'author_display': m.author_display,
                'body': m.body,
                'created_at': m.created_at.isoformat(),
            }
            for m in reversed(list(qs))
        ]

    @database_sync_to_async
    def _post_message(self, session_id, mode: str, body: str, agent_user_id=None) -> dict:
        from django.contrib.auth import get_user_model
        from .models import LiveChatSession
        from . import live_chat_services as lcs

        session = LiveChatSession.objects.select_related('ticket').get(pk=session_id)
        agent = None
        author_kind = 'customer'
        if mode == 'agent':
            author_kind = 'agent'
            agent = get_user_model().objects.filter(pk=agent_user_id).first()

        try:
            lcs.post_message(session, author_kind=author_kind, body=body, agent=agent)
            return {'ok': True}
        except Exception as exc:
            return {'ok': False, 'error': str(exc)}

    @staticmethod
    def _state_payload(session) -> dict:
        return {
            'kind': 'current',
            'is_active': session.is_active,
            'is_locked': session.is_locked,
            'lock_reason': session.lock_reason,
            'ticket_no': getattr(session.ticket, 'ticket_no', ''),
        }