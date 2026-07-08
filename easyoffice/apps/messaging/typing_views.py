"""
apps/messaging/typing_views.py
──────────────────────────────
Tiny endpoint that broadcasts a "typing" event over the existing chat
WebSocket group so the OTHER party's floating widget can render the
typing-indicator bubble.

Design
------
* Fire-and-forget — the frontend pings on every keystroke but throttles
  to ~1 POST / 3 seconds per room (see base.html → pingTyping()).
* No DB writes. Pure channel-layer fanout.
* Echoed to the entire chat_<room_id> group; the receiving client filters
  out its own user_id so it never sees its own dots.
* Failure modes are silent: if Channels / Redis isn't configured, the POST
  still 200s so the composer doesn't get stuck on a network error.
"""

import logging

from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.views.generic import View

from apps.messaging.models import ChatRoom

log = logging.getLogger(__name__)


def _safe_full_name(u):
    if not u:
        return ''
    name = (getattr(u, 'get_full_name', lambda: '')() or '').strip()
    return name or getattr(u, 'username', '') or ''


def _safe_initials(u):
    name = _safe_full_name(u)
    if not name:
        return '?'
    parts = [p for p in name.split() if p]
    if not parts:
        return '?'
    if len(parts) == 1:
        return parts[0][:1].upper()
    return (parts[0][:1] + parts[-1][:1]).upper()


def _avatar_url(user):
    if not user:
        return ''
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


class TypingPingView(LoginRequiredMixin, View):
    """
    POST /messages/<room_id>/typing/

    Broadcasts a chat.typing event to the chat_<room_id> group. The
    ChatConsumer must implement an async `chat_typing` method that
    forwards the event down the WebSocket as JSON. (See the patch
    snippet bundled with this change.)

    Body: none required (CSRF token via header or form field).
    Response: { "ok": true }  — always, even if the channel layer is
    unconfigured. We log the failure server-side; the UX should never
    block on a typing ping.
    """

    def post(self, request, room_id):
        # Membership check — only members of a room can signal typing in it.
        room = get_object_or_404(
            ChatRoom,
            id=room_id,
            members=request.user,
        )

        # 🔒 Same posting-permission rule as the message-send paths:
        # users who can't post (read-only room / read-only role) shouldn't
        # be able to broadcast typing indicators either.
        try:
            from apps.messaging.views import _can_post_in_room
            if not _can_post_in_room(request.user, room):
                return JsonResponse({'ok': True})  # silently ignore
        except Exception:
            pass

        try:
            from asgiref.sync import async_to_sync
            from channels.layers import get_channel_layer
            channel_layer = get_channel_layer()
            if channel_layer is None:
                log.warning(
                    'TypingPingView: get_channel_layer() returned None — '
                    'CHANNEL_LAYERS not configured. Typing indicator disabled.'
                )
                return JsonResponse({'ok': True})

            async_to_sync(channel_layer.group_send)(
                f'chat_{room.id}',
                {
                    'type': 'chat.typing',  # → ChatConsumer.chat_typing
                    'room_id': str(room.id),
                    'sender_id': str(request.user.id),
                    'sender_name': _safe_full_name(request.user),
                    'sender_initials': _safe_initials(request.user),
                    'sender_avatar_url': _avatar_url(request.user),
                },
            )
        except Exception:
            log.exception('TypingPingView: channel-layer broadcast failed')

        return JsonResponse({'ok': True})
