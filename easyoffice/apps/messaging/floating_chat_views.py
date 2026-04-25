"""
apps/messaging/floating_chat_views.py
─────────────────────────────────────
Lightweight endpoints that power the GLOBAL floating chat widget that
appears in the bottom-right corner of every authenticated page.

Design goals
------------
* Cheap — only ever returns up to 10 of TODAY's messages per room (the
  full history lives on /messages/<room_id>/).
* Tied to the existing _serialize_chat_message() so the payload shape is
  identical to what the in-room widget already understands.
* DM-only by default. Group / project channels still live on the main
  Messages page; the floating widget is for fast 1-on-1 replies.

Wire these up in apps/messaging/urls.py — see floating_chat_urls.py.
"""

import logging

from django.contrib.auth.mixins import LoginRequiredMixin
from django.views.generic import View
from django.shortcuts import get_object_or_404
from django.http import JsonResponse
from django.utils import timezone
from django.db.models import Q

from apps.messaging.models import (
    ChatRoom, ChatRoomMember, ChatMessage,
)
from apps.messaging.views import (
    _serialize_chat_message,
    _safe_full_name,
    _safe_initials,
    _save_mentions,
    _notify_offline_members,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

FLOATING_WIDGET_MESSAGE_LIMIT = 10  # "only 10 latest messages of that day"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _start_of_today_local():
    """Return today's midnight in the active timezone (used for 'today only')."""
    now = timezone.localtime(timezone.now())
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def _other_member(room, viewer):
    """For a DM room, return the *other* user (the one the viewer is talking to)."""
    if room.room_type != 'direct':
        return None
    return (
        room.members.exclude(id=viewer.id)
        .select_related('staffprofile')
        .first()
    )


def _avatar_url(user):
    """Best-effort user avatar URL — returns '' if none set."""
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


def _floating_room_summary(room, viewer):
    """
    Build a compact dict describing a single DM room for the floating
    widget's "open chats" list. Includes unread count + last message
    preview so we can show the inbox flyout the user described.
    """
    other = _other_member(room, viewer)

    try:
        membership = ChatRoomMember.objects.get(room=room, user=viewer)
        last_read = membership.last_read
    except ChatRoomMember.DoesNotExist:
        last_read = None

    unread_qs = room.messages.filter(is_deleted=False).exclude(sender=viewer)
    if last_read:
        unread_qs = unread_qs.filter(created_at__gt=last_read)
    unread = unread_qs.count()

    last_msg = (
        room.messages.filter(is_deleted=False)
        .select_related('sender')
        .order_by('-created_at')
        .first()
    )

    last_preview = ''
    last_at = ''
    if last_msg:
        # Models decrypt content via post_init, so msg.content is plaintext.
        if last_msg.message_type == 'file':
            last_preview = '📎 File'
        elif last_msg.message_type == 'image':
            last_preview = '🖼 Image'
        elif last_msg.message_type == 'poll':
            last_preview = '📊 Poll'
        else:
            last_preview = (last_msg.content or '')[:60]
        last_at = timezone.localtime(last_msg.created_at).isoformat()

    return {
        'room_id': str(room.id),
        'room_type': room.room_type,
        'title': _safe_full_name(other) if other else (room.name or 'Chat'),
        'subtitle': '',  # reserved for "typing…", role, etc.
        'avatar_url': _avatar_url(other) if other else '',
        'initials': _safe_initials(other) if other else '?',
        'other_user_id': str(other.id) if other else '',
        'unread': unread,
        'last_preview': last_preview,
        'last_at': last_at,
        'updated_at': timezone.localtime(room.updated_at).isoformat(),
    }


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

class FloatingChatInboxView(LoginRequiredMixin, View):
    """
    GET /messages/floating/inbox/

    Returns the user's DM rooms (plus a total unread count) for the
    floating widget's launcher and dropdown list. Used both for the
    initial paint and for periodic refreshes.

    Response:
        {
            "ok": true,
            "total_unread": 3,
            "rooms": [
                {room_id, title, avatar_url, initials, unread,
                 last_preview, last_at, updated_at, ...},
                ...
            ]
        }
    """

    def get(self, request):
        rooms_qs = (
            ChatRoom.objects.filter(
                members=request.user,
                is_archived=False,
                room_type='direct',
            )
            .prefetch_related('members')
            .order_by('-updated_at')
        )

        rooms = [_floating_room_summary(r, request.user) for r in rooms_qs]
        total_unread = sum(r['unread'] for r in rooms)

        return JsonResponse({
            'ok': True,
            'total_unread': total_unread,
            'rooms': rooms,
        })


class FloatingChatRoomMessagesView(LoginRequiredMixin, View):
    """
    GET /messages/floating/<room_id>/messages/

    Returns up to FLOATING_WIDGET_MESSAGE_LIMIT (10) of TODAY's messages
    for this room — newest 10, sorted oldest→newest for natural display.
    Anything older lives on the main /messages/<room_id>/ page.

    Optional query params:
        after_id=<uuid>   only return messages newer than this (for live
                          polling fallback when WebSockets are unavailable)

    Response:
        {
            "ok": true,
            "room": {room summary},
            "messages": [...serialized messages...],
            "has_more_today": false,
            "has_older": true
        }
    """

    def get(self, request, room_id):
        room = get_object_or_404(
            ChatRoom,
            id=room_id,
            members=request.user,
        )

        start_of_day = _start_of_today_local()
        after_id = (request.GET.get('after_id') or '').strip()

        base_qs = room.messages.filter(is_deleted=False)

        if after_id:
            # Incremental poll — return everything after this anchor that's
            # also from today. The widget only ever shows today, but we still
            # send anything newer in case the day has rolled over while the
            # tab was open.
            try:
                anchor = room.messages.get(id=after_id)
                qs = (
                    base_qs.filter(created_at__gt=anchor.created_at)
                    .select_related('sender', 'reply_to', 'reply_to__sender', 'linked_file')
                    .order_by('created_at')
                )
                messages_data = [_serialize_chat_message(m, viewer=request.user) for m in qs]
                return JsonResponse({
                    'ok': True,
                    'messages': messages_data,
                })
            except ChatMessage.DoesNotExist:
                pass  # fall through to full reload below

        # Full initial load: latest 10 from today.
        today_qs = base_qs.filter(created_at__gte=start_of_day)
        latest_today = list(
            today_qs
            .select_related('sender', 'reply_to', 'reply_to__sender', 'linked_file')
            .order_by('-created_at')[:FLOATING_WIDGET_MESSAGE_LIMIT]
        )
        latest_today.reverse()  # display oldest→newest

        # Mark as read up to now since the user is actively viewing them.
        ChatRoomMember.objects.filter(
            room=room,
            user=request.user,
        ).update(last_read=timezone.now())

        messages_data = [_serialize_chat_message(m, viewer=request.user) for m in latest_today]

        has_more_today = today_qs.count() > FLOATING_WIDGET_MESSAGE_LIMIT
        has_older = base_qs.filter(created_at__lt=start_of_day).exists()

        return JsonResponse({
            'ok': True,
            'room': _floating_room_summary(room, request.user),
            'messages': messages_data,
            'has_more_today': has_more_today,
            'has_older': has_older,
        })


class FloatingChatSendView(LoginRequiredMixin, View):
    """
    POST /messages/floating/<room_id>/send/

    Send a text message from the floating widget. Mirrors the WebSocket
    save path so the same room receives the same broadcast and the same
    serialized payload, regardless of which widget originated it.

    Body (form-urlencoded or JSON):
        content     required, text body (max 5000 chars enforced soft)
        reply_to    optional, message UUID to reply to

    Response:
        { "ok": true, "message": {...serialized...} }
    """

    def post(self, request, room_id):
        room = get_object_or_404(ChatRoom, id=room_id, members=request.user)

        if room.is_readonly:
            return JsonResponse({'ok': False, 'error': 'Room is read-only.'}, status=403)

        content = (request.POST.get('content') or request.POST.get('message') or '').strip()
        reply_to_id = (request.POST.get('reply_to') or '').strip()

        if not content:
            return JsonResponse({'ok': False, 'error': 'Empty message.'}, status=400)

        if len(content) > 5000:
            content = content[:5000]

        reply_obj = None
        if reply_to_id:
            try:
                reply_obj = ChatMessage.objects.get(
                    id=reply_to_id,
                    room=room,
                    is_deleted=False,
                )
            except ChatMessage.DoesNotExist:
                reply_obj = None

        msg = ChatMessage.objects.create(
            room=room,
            sender=request.user,
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

        ChatRoomMember.objects.filter(room=room, user=request.user).update(
            last_read=timezone.now()
        )

        # Broadcast through the same channel layer the in-room widget uses
        # so any open ChatConsumer subscriber (including the recipient on
        # any page) receives the message instantly.
        broadcast_ok = False
        try:
            from asgiref.sync import async_to_sync
            from channels.layers import get_channel_layer
            channel_layer = get_channel_layer()
            if channel_layer is None:
                log.error('floating_chat_send: get_channel_layer() returned None — '
                          'CHANNEL_LAYERS not configured? Live updates will not work.')
            else:
                payload = _serialize_chat_message(msg, viewer=request.user)
                async_to_sync(channel_layer.group_send)(
                    f'chat_{room.id}',
                    {'type': 'chat.message', 'payload': payload},
                )
                broadcast_ok = True
                log.info('floating_chat_send: broadcast to chat_%s OK', room.id)
        except Exception:
            log.exception('floating_chat_send: channel-layer broadcast FAILED')

        try:
            _notify_offline_members(room, request.user, msg)
        except Exception:
            pass

        return JsonResponse({
            'ok': True,
            'message': _serialize_chat_message(msg, viewer=request.user),
        })