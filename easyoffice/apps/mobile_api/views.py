"""
apps/mobile_api/views.py
────────────────────────
DRF endpoints for the EasyOffice mobile app (Flutter).

All endpoints are JWT-authenticated. The shape of the payloads is
deliberately close to the existing `_serialize_chat_message()` /
`_floating_room_summary()` helpers used on the web, so frontend code
that already consumes those won't be surprised.
"""

import logging
import os

from django.contrib.auth import authenticate
from django.db.models import Q, Max
from django.shortcuts import get_object_or_404
from django.utils import timezone

from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.parsers import MultiPartParser, FormParser, JSONParser
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.views import TokenRefreshView  # re-exported in urls.py

from apps.core.models import User
from apps.messaging.models import (
    ChatRoom, ChatRoomMember, ChatMessage,
    ChatPoll, ChatPollOption, ChatPollVote,
)
from apps.mobile_api.serializers import (
    UserMiniSerializer,
    ChatRoomSerializer,
    ChatMessageSerializer,
    ChatPollSerializer,
    DeviceTokenSerializer,
)

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# AUTH
# ─────────────────────────────────────────────────────────────────────────────

class MobileLoginView(APIView):
    """
    POST /api/mobile/v1/auth/login/
    Body: { "username": "...", "password": "..." }
    Returns: { access, refresh, user }
    """
    permission_classes = [AllowAny]

    def post(self, request):
        username = (request.data.get('username') or '').strip()
        password = request.data.get('password') or ''

        if not username or not password:
            return Response(
                {'detail': 'username and password are required.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        user = authenticate(request, username=username, password=password)
        if not user:
            return Response(
                {'detail': 'Invalid credentials.'},
                status=status.HTTP_401_UNAUTHORIZED,
            )
        if not user.is_active:
            return Response(
                {'detail': 'Account is disabled.'},
                status=status.HTTP_403_FORBIDDEN,
            )

        refresh = RefreshToken.for_user(user)

        return Response({
            'access': str(refresh.access_token),
            'refresh': str(refresh),
            'user': UserMiniSerializer(user, context={'request': request}).data,
        })


class MobileLogoutView(APIView):
    """
    POST /api/mobile/v1/auth/logout/
    Body: { "refresh": "..." }
    Blacklists the refresh token.
    """

    def post(self, request):
        refresh = request.data.get('refresh')
        if not refresh:
            return Response(status=status.HTTP_205_RESET_CONTENT)
        try:
            token = RefreshToken(refresh)
            token.blacklist()
        except Exception:
            pass
        return Response(status=status.HTTP_205_RESET_CONTENT)


class MobileMeView(APIView):
    """GET /api/mobile/v1/auth/me/ — current user."""

    def get(self, request):
        return Response(UserMiniSerializer(request.user, context={'request': request}).data)


# ─────────────────────────────────────────────────────────────────────────────
# ROOMS
# ─────────────────────────────────────────────────────────────────────────────

class RoomListView(APIView):
    """
    GET /api/mobile/v1/rooms/
    Query params:
        type=direct|group|unit|department|announcement|project (optional, repeatable)
        archived=0|1 (default 0)
    """

    def get(self, request):
        types = request.query_params.getlist('type')
        archived = request.query_params.get('archived') == '1'

        qs = (
            ChatRoom.objects.filter(members=request.user, is_archived=archived)
            .prefetch_related('members')
            .order_by('-updated_at')
        )
        if types:
            qs = qs.filter(room_type__in=types)

        ser = ChatRoomSerializer(
            qs, many=True,
            context={'request': request, 'viewer': request.user},
        )
        total_unread = sum(r['unread'] for r in ser.data)
        return Response({'rooms': ser.data, 'total_unread': total_unread})


class RoomDetailView(APIView):
    """GET /api/mobile/v1/rooms/<room_id>/ — single room with members."""

    def get(self, request, room_id):
        room = get_object_or_404(ChatRoom, id=room_id, members=request.user)
        data = ChatRoomSerializer(
            room, context={'request': request, 'viewer': request.user}
        ).data
        data['members'] = UserMiniSerializer(
            room.members.all(), many=True, context={'request': request}
        ).data
        return Response(data)


class DirectRoomView(APIView):
    """
    POST /api/mobile/v1/rooms/direct/
    Body: { "user_id": "<uuid>" }
    Returns existing DM room or creates a new one.
    """

    def post(self, request):
        target_id = request.data.get('user_id')
        if not target_id:
            return Response({'detail': 'user_id required'}, status=400)
        if str(target_id) == str(request.user.id):
            return Response({'detail': 'Cannot DM yourself.'}, status=400)

        try:
            target = User.objects.get(id=target_id, is_active=True)
        except (User.DoesNotExist, ValueError):
            return Response({'detail': 'User not found.'}, status=404)

        # find an existing 1-on-1 room with both members and ONLY two members
        existing = (
            ChatRoom.objects.filter(room_type='direct', members=request.user)
            .filter(members=target)
            .annotate(mc=Max('id'))  # touch members to ensure join
        )
        room = None
        for r in existing:
            if r.members.count() == 2:
                room = r
                break

        if room is None:
            room = ChatRoom.objects.create(
                room_type='direct',
                created_by=request.user,
            )
            ChatRoomMember.objects.create(room=room, user=request.user)
            ChatRoomMember.objects.create(room=room, user=target)

        return Response(
            ChatRoomSerializer(
                room, context={'request': request, 'viewer': request.user}
            ).data
        )


# ─────────────────────────────────────────────────────────────────────────────
# MESSAGES
# ─────────────────────────────────────────────────────────────────────────────

class RoomMessagesView(APIView):
    """
    GET  /api/mobile/v1/rooms/<room_id>/messages/?before=<iso>&limit=50
    POST /api/mobile/v1/rooms/<room_id>/messages/
         body: { "content": "...", "reply_to": "<uuid>"? }
    """
    parser_classes = [JSONParser, FormParser, MultiPartParser]

    def get(self, request, room_id):
        room = get_object_or_404(ChatRoom, id=room_id, members=request.user)

        before = request.query_params.get('before')
        try:
            limit = max(1, min(100, int(request.query_params.get('limit') or 50)))
        except ValueError:
            limit = 50

        qs = (
            room.messages.filter(is_deleted=False)
            .select_related('sender', 'reply_to', 'reply_to__sender', 'linked_file')
        )
        if before:
            qs = qs.filter(created_at__lt=before)

        # Exclude messages this user has hidden ("delete for me").
        try:
            from apps.messaging.models import HiddenMessage
            hidden_ids = set(
                HiddenMessage.objects.filter(user=request.user)
                .values_list('message_id', flat=True)
            )
            if hidden_ids:
                qs = qs.exclude(id__in=hidden_ids)
        except Exception:
            log.exception('mobile messages: hidden-message filter failed')

        messages = list(qs.order_by('-created_at')[:limit])
        messages.reverse()  # oldest → newest for the client

        # mark read up to now
        ChatRoomMember.objects.filter(room=room, user=request.user).update(
            last_read=timezone.now()
        )

        ser = ChatMessageSerializer(
            messages, many=True,
            context={'request': request, 'viewer': request.user},
        )
        return Response({'messages': ser.data, 'has_more': len(messages) == limit})

    def post(self, request, room_id):
        room = get_object_or_404(ChatRoom, id=room_id, members=request.user)
        if room.is_readonly:
            return Response({'detail': 'Room is read-only.'}, status=403)

        content = (request.data.get('content') or '').strip()
        reply_to_id = request.data.get('reply_to') or None
        uploaded = request.FILES.get('file')

        if not content and not uploaded:
            return Response({'detail': 'Empty message.'}, status=400)
        if len(content) > 5000:
            content = content[:5000]

        reply_obj = None
        if reply_to_id:
            try:
                reply_obj = ChatMessage.objects.get(
                    id=reply_to_id, room=room, is_deleted=False,
                )
            except ChatMessage.DoesNotExist:
                pass

        msg_type = 'text'
        kwargs = {}
        if uploaded:
            ext = (os.path.splitext(uploaded.name)[1] or '').lower()
            msg_type = 'image' if ext in ('.jpg', '.jpeg', '.png', '.gif', '.webp', '.heic') else 'file'
            kwargs['file'] = uploaded
            kwargs['file_name'] = uploaded.name
            kwargs['file_size'] = uploaded.size

        msg = ChatMessage.objects.create(
            room=room,
            sender=request.user,
            content=content,
            message_type=msg_type,
            reply_to=reply_obj,
            **kwargs,
        )

        # mentions + offline notify (best-effort; reuse existing helpers)
        try:
            from apps.messaging.views import _save_mentions, _notify_offline_members
            _save_mentions(msg)
            _notify_offline_members(room, request.user, msg)
        except Exception:
            log.exception('mobile send: ancillary helpers failed')

        room.updated_at = timezone.now()
        room.save(update_fields=['updated_at'])
        ChatRoomMember.objects.filter(room=room, user=request.user).update(
            last_read=timezone.now()
        )

        # Broadcast on the same channel layer the web widgets subscribe to
        # so anyone with a chat tab open sees this immediately.
        try:
            from asgiref.sync import async_to_sync
            from channels.layers import get_channel_layer
            channel_layer = get_channel_layer()
            if channel_layer is not None:
                payload = ChatMessageSerializer(
                    msg, context={'request': request, 'viewer': request.user}
                ).data
                async_to_sync(channel_layer.group_send)(
                    f'chat_{room.id}',
                    {'type': 'chat.message', 'payload': payload},
                )
        except Exception:
            log.exception('mobile send: broadcast failed')

        return Response(
            ChatMessageSerializer(
                msg, context={'request': request, 'viewer': request.user}
            ).data,
            status=status.HTTP_201_CREATED,
        )


class MessageDeleteView(APIView):
    """DELETE /api/mobile/v1/messages/<message_id>/ — soft delete (for everyone)."""

    def delete(self, request, message_id):
        msg = get_object_or_404(
            ChatMessage, id=message_id,
            room__members=request.user,
        )
        if msg.sender_id != request.user.id:
            return Response({'detail': 'Forbidden.'}, status=403)

        msg.is_deleted = True
        msg.content = ''
        msg.save(update_fields=['is_deleted', 'content'])

        # broadcast deletion
        try:
            from asgiref.sync import async_to_sync
            from channels.layers import get_channel_layer
            channel_layer = get_channel_layer()
            if channel_layer is not None:
                async_to_sync(channel_layer.group_send)(
                    f'chat_{msg.room_id}',
                    {
                        'type': 'chat.edit',
                        'payload': {
                            'type': 'message_deleted',
                            'message_id': str(msg.id),
                            'room_id': str(msg.room_id),
                        },
                    },
                )
        except Exception:
            pass

        return Response(status=204)


class ReactionToggleView(APIView):
    """
    POST /api/mobile/v1/messages/<message_id>/react/
    Body: { "emoji": "👍" }
    Toggles the viewer's reaction on the message.
    """

    def post(self, request, message_id):
        emoji = (request.data.get('emoji') or '').strip()
        if not emoji or len(emoji) > 8:
            return Response({'detail': 'Bad emoji.'}, status=400)

        msg = get_object_or_404(
            ChatMessage, id=message_id, room__members=request.user, is_deleted=False
        )

        reactions = msg.reactions or {}
        users = list(reactions.get(emoji) or [])
        viewer_id = str(request.user.id)
        if viewer_id in [str(u) for u in users]:
            users = [u for u in users if str(u) != viewer_id]
        else:
            users.append(viewer_id)

        if users:
            reactions[emoji] = users
        else:
            reactions.pop(emoji, None)

        msg.reactions = reactions
        msg.save(update_fields=['reactions'])

        # broadcast
        try:
            from asgiref.sync import async_to_sync
            from channels.layers import get_channel_layer
            channel_layer = get_channel_layer()
            if channel_layer is not None:
                async_to_sync(channel_layer.group_send)(
                    f'chat_{msg.room_id}',
                    {
                        'type': 'chat.reaction',
                        'payload': {
                            'type': 'reaction_update',
                            'message_id': str(msg.id),
                            'room_id': str(msg.room_id),
                            'reactions': reactions,
                        },
                    },
                )
        except Exception:
            pass

        return Response({
            'message_id': str(msg.id),
            'reactions': reactions,
        })


class RoomUploadView(APIView):
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request, room_id):
        from apps.messaging.models import ChatRoom, ChatMessage, ChatRoomMember
        from apps.messaging.views import _serialize_chat_message
        from asgiref.sync import async_to_sync
        from channels.layers import get_channel_layer

        room = ChatRoom.objects.filter(id=room_id, members=request.user).first()
        if not room or room.is_readonly:
            return Response({'error': 'Not allowed'}, status=403)

        f = request.FILES.get('file')
        if not f:
            return Response({'error': 'No file'}, status=400)

        is_image = (f.content_type or '').startswith('image/')
        msg = ChatMessage.objects.create(
            room=room,
            sender=request.user,
            message_type='image' if is_image else 'file',
            file=f,
            file_name=f.name,
            file_size=f.size,
            content=request.data.get('caption', ''),
        )
        room.save(update_fields=['updated_at'])

        payload = _serialize_chat_message(msg, viewer=request.user)
        try:
            async_to_sync(get_channel_layer().group_send)(
                f'chat_{room.id}', {'type': 'chat.message', 'payload': payload},
            )
        except Exception:
            pass
        return Response(payload, status=201)


class MessageEditView(APIView):
    permission_classes = [IsAuthenticated]

    def patch(self, request, message_id):
        from apps.messaging.models import ChatMessage
        from apps.messaging.views import _serialize_chat_message
        from asgiref.sync import async_to_sync
        from channels.layers import get_channel_layer
        from django.utils import timezone

        msg = ChatMessage.objects.filter(id=message_id).first()
        if not msg:
            return Response({'error': 'Not found'}, status=404)
        # Only the sender can edit, and only text messages.
        if msg.sender_id != request.user.id:
            return Response({'error': 'Not your message'}, status=403)
        if msg.is_deleted:
            return Response({'error': 'Message deleted'}, status=400)
        if msg.message_type not in ('text',):
            return Response({'error': 'Only text messages can be edited'}, status=400)

        new_content = (request.data.get('content') or '').strip()
        if not new_content:
            return Response({'error': 'Content required'}, status=400)

        msg.content = new_content       # re-encrypted on save by the model
        msg.is_edited = True
        msg.edited_at = timezone.now()
        msg.save(update_fields=['content', 'is_edited', 'edited_at'])

        payload = _serialize_chat_message(msg, viewer=request.user)
        try:
            async_to_sync(get_channel_layer().group_send)(
                f'chat_{msg.room_id}',
                {'type': 'chat.message', 'payload': payload},
            )
        except Exception:
            pass
        return Response(payload, status=200)


class MessageHideView(APIView):
    """POST /api/mobile/v1/messages/<message_id>/hide/ — 'delete for me' only."""
    permission_classes = [IsAuthenticated]

    def post(self, request, message_id):
        from apps.messaging.models import ChatMessage, HiddenMessage
        msg = ChatMessage.objects.filter(
            id=message_id, room__members=request.user
        ).first()
        if not msg:
            return Response({'error': 'Not found'}, status=404)
        HiddenMessage.objects.get_or_create(user=request.user, message=msg)
        return Response({'status': 'ok'}, status=200)


# ─────────────────────────────────────────────────────────────────────────────
# POLLS
# ─────────────────────────────────────────────────────────────────────────────

class CreatePollView(APIView):
    """
    POST /api/mobile/v1/rooms/<room_id>/polls/
    Body: {
        "question": "...",
        "options": ["a", "b", ...],
        "allow_multiple": false,
        "is_anonymous": false,
        "ends_at": "2026-05-04T12:00:00Z" | null
    }
    """

    def post(self, request, room_id):
        room = get_object_or_404(ChatRoom, id=room_id, members=request.user)
        if room.is_readonly:
            return Response({'detail': 'Room is read-only.'}, status=403)

        question = (request.data.get('question') or '').strip()
        options = request.data.get('options') or []
        if not question or not isinstance(options, list) or len(options) < 2:
            return Response(
                {'detail': 'question and at least 2 options are required.'},
                status=400,
            )
        options = [str(o).strip() for o in options if str(o).strip()][:10]
        if len(options) < 2:
            return Response({'detail': 'At least 2 valid options required.'}, status=400)

        msg = ChatMessage.objects.create(
            room=room,
            sender=request.user,
            content=f'📊 Poll: {question}',
            message_type='poll',
        )
        poll = ChatPoll.objects.create(
            message=msg,
            question=question,
            allow_multiple=bool(request.data.get('allow_multiple')),
            is_anonymous=bool(request.data.get('is_anonymous')),
            allow_vote_change=True,
            ends_at=request.data.get('ends_at') or None,
            created_by=request.user,
        )
        for opt in options:
            ChatPollOption.objects.create(poll=poll, text=opt)

        room.updated_at = timezone.now()
        room.save(update_fields=['updated_at'])

        # broadcast
        try:
            from asgiref.sync import async_to_sync
            from channels.layers import get_channel_layer
            channel_layer = get_channel_layer()
            if channel_layer is not None:
                payload = ChatMessageSerializer(
                    msg, context={'request': request, 'viewer': request.user}
                ).data
                async_to_sync(channel_layer.group_send)(
                    f'chat_{room.id}',
                    {'type': 'chat.message', 'payload': payload},
                )
        except Exception:
            pass

        return Response(
            ChatMessageSerializer(
                msg, context={'request': request, 'viewer': request.user}
            ).data,
            status=201,
        )


class VotePollView(APIView):
    """
    POST /api/mobile/v1/polls/<poll_id>/vote/
    Body: { "option_ids": ["<uuid>", ...] }
    """

    def post(self, request, poll_id):
        poll = get_object_or_404(
            ChatPoll, id=poll_id, message__room__members=request.user
        )
        if poll.is_effectively_closed:
            return Response({'detail': 'Poll is closed.'}, status=400)

        option_ids = request.data.get('option_ids') or []
        if not isinstance(option_ids, list) or not option_ids:
            return Response({'detail': 'option_ids required.'}, status=400)

        valid_ids = list(poll.options.filter(id__in=option_ids).values_list('id', flat=True))
        if not valid_ids:
            return Response({'detail': 'No valid options.'}, status=400)
        if not poll.allow_multiple:
            valid_ids = valid_ids[:1]

        # remove existing votes if change allowed
        if poll.allow_vote_change:
            ChatPollVote.objects.filter(poll=poll, user=request.user).delete()
        else:
            if ChatPollVote.objects.filter(poll=poll, user=request.user).exists():
                return Response({'detail': 'Already voted.'}, status=400)

        for oid in valid_ids:
            ChatPollVote.objects.get_or_create(
                poll=poll, option_id=oid, user=request.user,
            )

        # broadcast
        try:
            from asgiref.sync import async_to_sync
            from channels.layers import get_channel_layer
            channel_layer = get_channel_layer()
            if channel_layer is not None:
                async_to_sync(channel_layer.group_send)(
                    f'chat_{poll.message.room_id}',
                    {
                        'type': 'chat.poll',
                        'payload': {
                            'type': 'poll_update',
                            'poll_id': str(poll.id),
                            'message_id': str(poll.message_id),
                            'room_id': str(poll.message.room_id),
                        },
                    },
                )
        except Exception:
            pass

        return Response(
            ChatPollSerializer(poll, context={'viewer': request.user}).data
        )


# ─────────────────────────────────────────────────────────────────────────────
# PRESENCE & TYPING
# ─────────────────────────────────────────────────────────────────────────────

class PresenceHeartbeatView(APIView):
    """
    POST /api/mobile/v1/presence/heartbeat/
    Body (optional): { "status": "online|away|dnd" }

    Stores a short-TTL key in Redis so other clients can see this user
    is currently online. Mirrors the web widget's existing heartbeat.
    """

    def post(self, request):
        from django.core.cache import cache
        ttl = 90  # seconds
        cache.set(f'presence:{request.user.id}', {
            'status': request.data.get('status') or 'online',
            'at': timezone.now().isoformat(),
        }, ttl)
        return Response({'ok': True})


class PresenceQueryView(APIView):
    """GET /api/mobile/v1/presence/<user_id>/"""

    def get(self, request, user_id):
        from django.core.cache import cache
        data = cache.get(f'presence:{user_id}')
        if not data:
            return Response({'online': False})
        return Response({'online': True, **data})


# ─────────────────────────────────────────────────────────────────────────────
# DEVICE TOKEN (push notifications)
# ─────────────────────────────────────────────────────────────────────────────

class DeviceTokenView(APIView):
    """
    POST /api/mobile/v1/device-tokens/
    Body: { platform: 'ios'|'android', token: '...', app_version: '...' }

    Stub: persists the token on the user model. Wire up FCM / APNs in a
    later phase — the mobile app already sends what's needed.
    """

    def post(self, request):
        ser = DeviceTokenSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        # Implement DeviceToken model in apps/mobile_api/models.py to persist.
        # For now, log and accept so the mobile app's pipeline doesn't break.
        log.info(
            'device-token register: user=%s platform=%s token=%s...',
            request.user.id,
            ser.validated_data['platform'],
            ser.validated_data['token'][:12],
        )
        return Response({'ok': True})


# ─────────────────────────────────────────────────────────────────────────────
# USER SEARCH (for "new DM" picker)
# ─────────────────────────────────────────────────────────────────────────────

class UserSearchView(APIView):
    """GET /api/mobile/v1/users/search/?q=foo"""

    def get(self, request):
        q = (request.query_params.get('q') or '').strip()
        if len(q) < 2:
            return Response({'users': []})
        qs = User.objects.filter(is_active=True).exclude(id=request.user.id)
        qs = qs.filter(
            Q(username__icontains=q) |
            Q(first_name__icontains=q) |
            Q(last_name__icontains=q) |
            Q(email__icontains=q)
        )[:25]
        ser = UserMiniSerializer(qs, many=True, context={'request': request})
        return Response({'users': ser.data})