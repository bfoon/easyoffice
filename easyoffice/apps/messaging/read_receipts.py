"""
apps/messaging/read_receipts.py
───────────────────────────────
Delivery + read receipts for chat (WhatsApp-style ticks).

Design: WATERMARKS, not per-message rows
----------------------------------------
A per-message ``MessageRead`` table would mean one row per (message ×
recipient). A 40-person channel with 200 messages/day is 8,000 rows/day
for a feature nobody queries historically. Instead we store two
timestamps per (room, user) on the EXISTING ``ChatRoomMember`` row:

    last_delivered  — the peer's socket has received everything up to here
    last_read       — the peer has actually looked at everything up to here

A message is then:

    ✓        sent          (it exists)
    ✓✓ grey  delivered     recipient.last_delivered >= message.created_at
    ✓✓ blue  read          recipient.last_read      >= message.created_at

That is O(members) state per room instead of O(messages × members), it
survives history pagination for free, and the client already has
``data-ts`` on every bubble so it can colour ticks with no extra lookup.

Both watermarks move MONOTONICALLY FORWARD ONLY. Opening an old room,
a late-arriving socket event, or a clock skew can never "unread" a
message that was already read.

Wiring
------
* ``ChatConsumer.connect()``  → mark_delivered()  (socket is live)
* ``ChatConsumer.receive({'type':'read'})`` → mark_read()  (no HTTP hop)
* ``POST /messages/<room_id>/read/``      → mark_read()  (REST fallback)
* ``GET  /messages/<room_id>/read-state/``→ initial paint + poll fallback

Requires ONE new model field — see models.py:

    class ChatRoomMember(models.Model):
        ...
        last_delivered = models.DateTimeField(null=True, blank=True)

then:  python manage.py makemigrations messaging && python manage.py migrate

Privacy note
------------
Read receipts tell colleagues when you opened a message. In a UN office
that is a real (if small) surveillance surface. ``MESSAGING_READ_RECEIPTS_ENABLED``
turns the whole feature off globally; ``ChatRoom.room_type`` filtering
below restricts nothing by default, but see ROOM_TYPES_WITH_RECEIPTS if
you want receipts on DMs only.
"""

import logging

from django.conf import settings
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.views.generic import View

from apps.messaging.models import ChatRoom, ChatRoomMember, ChatMessage

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def _receipts_enabled():
    return bool(getattr(settings, 'MESSAGING_READ_RECEIPTS_ENABLED', True))


# Set to a tuple like ('direct',) to limit receipts to DMs only.
ROOM_TYPES_WITH_RECEIPTS = getattr(
    settings, 'MESSAGING_READ_RECEIPT_ROOM_TYPES', None
)


def _room_has_receipts(room):
    if not _receipts_enabled():
        return False
    if ROOM_TYPES_WITH_RECEIPTS:
        return room.room_type in ROOM_TYPES_WITH_RECEIPTS
    return True


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _room_group_name(room_id):
    return f'chat_{room_id}'


def _epoch(dt):
    """Unix seconds, matching the ``data-ts`` attribute the template emits."""
    if not dt:
        return 0
    try:
        return int(dt.timestamp())
    except Exception:
        return 0


def _display_name(user):
    # Lazy import: views.py imports models, and urls.py imports views before
    # this module — keep the dependency inside the call to stay circular-safe.
    try:
        from apps.messaging.views import _safe_full_name
        return _safe_full_name(user)
    except Exception:
        return str(user)


def _display_initials(user):
    try:
        from apps.messaging.views import _safe_initials
        return _safe_initials(user)
    except Exception:
        return '?'


# ---------------------------------------------------------------------------
# Broadcast
# ---------------------------------------------------------------------------

def broadcast_receipt(room_id, user, delivered_at=None, read_at=None):
    """
    Fan a receipt update out to everyone with an open socket on this room.

    Silent on failure — a missing channel layer must never break a message
    save or a page render. The REST /read-state/ endpoint is the fallback.
    """
    try:
        from asgiref.sync import async_to_sync
        from channels.layers import get_channel_layer

        layer = get_channel_layer()
        if layer is None:
            return

        async_to_sync(layer.group_send)(
            _room_group_name(room_id),
            {
                'type': 'chat.receipt',
                'payload': {
                    'type': 'read_receipt',
                    'room_id': str(room_id),
                    'user_id': str(user.id),
                    'user_name': _display_name(user),
                    'delivered_ts': _epoch(delivered_at),
                    'read_ts': _epoch(read_at),
                },
            },
        )
    except Exception:
        log.exception('broadcast_receipt failed for room %s', room_id)


# ---------------------------------------------------------------------------
# Watermark mutation (monotonic)
# ---------------------------------------------------------------------------

def mark_delivered(room, user, when=None, broadcast=True):
    """
    Advance *user*'s delivered watermark in *room* to *when* (default: now).

    Called when the user's WebSocket attaches to the room group — at that
    instant every message already in the room has physically reached a
    live client for this user.

    Returns the new watermark, or None when nothing moved.
    """
    if not _room_has_receipts(room):
        return None

    when = when or timezone.now()

    moved = (
        ChatRoomMember.objects
        .filter(room=room, user=user)
        .filter(Q(last_delivered__isnull=True) | Q(last_delivered__lt=when))
        .update(last_delivered=when)
    )
    if not moved:
        return None

    if broadcast:
        broadcast_receipt(room.id, user, delivered_at=when)
    return when


def mark_read(room, user, when=None, broadcast=True):
    """
    Advance *user*'s read watermark in *room* to *when* (default: now).

    Read implies delivered, so ``last_delivered`` is dragged forward with
    it — otherwise a REST-only client (no socket) would show ✓✓ blue while
    the grey delivered state lagged behind, which looks like a bug.

    Returns the new watermark, or None when nothing moved.
    """
    if not _room_has_receipts(room):
        return None

    when = when or timezone.now()

    moved = (
        ChatRoomMember.objects
        .filter(room=room, user=user)
        .filter(Q(last_read__isnull=True) | Q(last_read__lt=when))
        .update(last_read=when)
    )

    # Keep delivered >= read regardless of whether read actually moved.
    ChatRoomMember.objects.filter(room=room, user=user).filter(
        Q(last_delivered__isnull=True) | Q(last_delivered__lt=when)
    ).update(last_delivered=when)

    if not moved:
        return None

    if broadcast:
        broadcast_receipt(room.id, user, delivered_at=when, read_at=when)
    return when


def mark_read_up_to_message(room, user, message_id, broadcast=True):
    """
    Advance the read watermark to a SPECIFIC message's timestamp.

    Used by the client when it can see exactly which bubble is the last one
    scrolled into view — more honest than "now", because "now" would mark
    messages read that arrived while the user was scrolled up in history.
    """
    try:
        msg = ChatMessage.objects.only('created_at').get(id=message_id, room=room)
    except (ChatMessage.DoesNotExist, ValueError, TypeError):
        return None
    return mark_read(room, user, when=msg.created_at, broadcast=broadcast)


# ---------------------------------------------------------------------------
# Read state (for initial paint / polling fallback)
# ---------------------------------------------------------------------------

def read_state(room, viewer):
    """
    Return every member's watermarks so the client can colour its ticks.

    Shape:
        {
            "enabled": true,
            "room_id": "...",
            "room_type": "direct",
            "members": [
                {"user_id","name","initials","delivered_ts","read_ts","is_self"},
                ...
            ]
        }
    """
    if not _room_has_receipts(room):
        return {'enabled': False, 'room_id': str(room.id), 'members': []}

    rows = (
        ChatRoomMember.objects
        .filter(room=room)
        .select_related('user')
    )

    members = []
    for row in rows:
        if not row.user_id:
            continue
        members.append({
            'user_id':      str(row.user_id),
            'name':         _display_name(row.user),
            'initials':     _display_initials(row.user),
            'delivered_ts': _epoch(row.last_delivered),
            'read_ts':      _epoch(row.last_read),
            'is_self':      str(row.user_id) == str(viewer.id),
        })

    return {
        'enabled':   True,
        'room_id':   str(room.id),
        'room_type': room.room_type,
        'members':   members,
    }


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

class ReadStateView(LoginRequiredMixin, View):
    """
    GET /messages/<room_id>/read-state/

    Initial paint after page load, and re-sync after a socket reconnect.
    Cheap: one query over the membership table, no message scan.
    """

    def get(self, request, room_id):
        room = get_object_or_404(ChatRoom, id=room_id, members=request.user)
        return JsonResponse({'ok': True, **read_state(room, request.user)})


class MarkReadView(LoginRequiredMixin, View):
    """
    POST /messages/<room_id>/read/

    Body (optional):
        up_to=<message uuid>   mark read up to that message's timestamp
                               (omit to mark everything read as of now)

    Response:
        { "ok": true, "read_ts": 1753800000 }

    Always 200s — a failed receipt must never surface as an error toast in
    the composer. The WebSocket path (``{"type":"read"}``) is preferred;
    this exists for the floating widget and for browsers where the socket
    is down and the poll fallback is driving the UI.
    """

    def post(self, request, room_id):
        room = get_object_or_404(ChatRoom, id=room_id, members=request.user)

        up_to = (request.POST.get('up_to') or '').strip()

        if up_to:
            new_ts = mark_read_up_to_message(room, request.user, up_to)
        else:
            new_ts = mark_read(room, request.user)

        return JsonResponse({'ok': True, 'read_ts': _epoch(new_ts)})
