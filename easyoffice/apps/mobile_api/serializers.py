"""
apps/mobile_api/serializers.py
──────────────────────────────
DRF serializers for the EasyOffice mobile app.

These deliberately produce a payload shape compatible with what the web
widget already consumes via `_serialize_chat_message()` — so the mobile
team and the web team share the same mental model.
"""

from rest_framework import serializers
from django.utils import timezone

from apps.core.models import User
from apps.messaging.models import (
    ChatRoom, ChatRoomMember, ChatMessage,
    ChatPoll, ChatPollOption, ChatPollVote,
)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _safe_full_name(user):
    if not user:
        return ''
    try:
        return user.full_name or user.get_full_name() or user.username
    except Exception:
        return str(user)


def _safe_initials(user):
    if not user:
        return '?'
    try:
        if getattr(user, 'initials', None):
            return user.initials
    except Exception:
        pass
    name = _safe_full_name(user).strip()
    if not name:
        return '?'
    parts = name.split()
    return ''.join(p[0].upper() for p in parts[:2]) or '?'


def _avatar_url(user, request=None):
    if not user:
        return ''
    url = ''
    try:
        if getattr(user, 'avatar', None) and user.avatar:
            url = user.avatar.url
    except Exception:
        pass
    if not url:
        try:
            sp = getattr(user, 'staffprofile', None)
            if sp and getattr(sp, 'profile_picture', None) and sp.profile_picture:
                url = sp.profile_picture.url
        except Exception:
            pass
    if url and request is not None:
        try:
            url = request.build_absolute_uri(url)
        except Exception:
            pass
    return url


# ─────────────────────────────────────────────────────────────────────────────
# User
# ─────────────────────────────────────────────────────────────────────────────

class UserMiniSerializer(serializers.ModelSerializer):
    full_name = serializers.SerializerMethodField()
    initials = serializers.SerializerMethodField()
    avatar_url = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = ['id', 'username', 'full_name', 'initials', 'avatar_url']

    def get_full_name(self, obj):
        return _safe_full_name(obj)

    def get_initials(self, obj):
        return _safe_initials(obj)

    def get_avatar_url(self, obj):
        return _avatar_url(obj, request=self.context.get('request'))


# ─────────────────────────────────────────────────────────────────────────────
# Polls
# ─────────────────────────────────────────────────────────────────────────────

class ChatPollOptionSerializer(serializers.ModelSerializer):
    vote_count = serializers.IntegerField(read_only=True)
    voted_by_me = serializers.SerializerMethodField()

    class Meta:
        model = ChatPollOption
        fields = ['id', 'text', 'vote_count', 'voted_by_me']

    def get_voted_by_me(self, obj):
        viewer = self.context.get('viewer')
        if not viewer or not viewer.is_authenticated:
            return False
        return ChatPollVote.objects.filter(option=obj, user=viewer).exists()


class ChatPollSerializer(serializers.ModelSerializer):
    options = ChatPollOptionSerializer(many=True, read_only=True)
    is_effectively_closed = serializers.BooleanField(read_only=True)
    total_votes = serializers.SerializerMethodField()

    class Meta:
        model = ChatPoll
        fields = [
            'id', 'question', 'allow_multiple', 'is_anonymous',
            'allow_vote_change', 'is_closed', 'is_effectively_closed',
            'ends_at', 'options', 'total_votes',
        ]

    def get_total_votes(self, obj):
        return ChatPollVote.objects.filter(poll=obj).count()


# ─────────────────────────────────────────────────────────────────────────────
# Messages
# ─────────────────────────────────────────────────────────────────────────────

class ChatMessageSerializer(serializers.ModelSerializer):
    sender = UserMiniSerializer(read_only=True)
    reply_to = serializers.SerializerMethodField()
    file_url = serializers.SerializerMethodField()
    file_name = serializers.SerializerMethodField()
    file_size = serializers.SerializerMethodField()
    poll = serializers.SerializerMethodField()
    reactions_summary = serializers.SerializerMethodField()
    is_mine = serializers.SerializerMethodField()

    class Meta:
        model = ChatMessage
        fields = [
            'id', 'room_id', 'sender', 'content', 'message_type',
            'file_url', 'file_name', 'file_size',
            'reactions', 'reactions_summary',
            'is_deleted', 'is_edited',
            'created_at', 'edited_at',
            'reply_to', 'poll', 'is_mine',
        ]

    def get_reply_to(self, obj):
        if not obj.reply_to_id or not obj.reply_to:
            return None
        rt = obj.reply_to
        return {
            'id': str(rt.id),
            'sender_name': _safe_full_name(rt.sender),
            'content': (rt.content or '')[:120],
            'message_type': rt.message_type,
        }

    def get_file_url(self, obj):
        url = obj.effective_file_url
        if not url:
            return ''
        request = self.context.get('request')
        if request is not None:
            try:
                return request.build_absolute_uri(url)
            except Exception:
                pass
        return url

    def get_file_name(self, obj):
        return obj.effective_file_name

    def get_file_size(self, obj):
        return obj.effective_file_size

    def get_poll(self, obj):
        if obj.message_type != 'poll':
            return None
        try:
            poll = obj.poll
        except ChatPoll.DoesNotExist:
            return None
        return ChatPollSerializer(poll, context=self.context).data

    def get_reactions_summary(self, obj):
        """
        ChatMessage.reactions is a JSONField mapping emoji -> [user_id, ...].
        Return a list of {emoji, count, mine} for the mobile UI.
        """
        viewer = self.context.get('viewer')
        viewer_id = str(viewer.id) if viewer and viewer.is_authenticated else None
        out = []
        reactions = obj.reactions or {}
        if not isinstance(reactions, dict):
            return out
        for emoji, users in reactions.items():
            users = users or []
            mine = viewer_id is not None and viewer_id in [str(u) for u in users]
            out.append({'emoji': emoji, 'count': len(users), 'mine': mine})
        return out

    def get_is_mine(self, obj):
        viewer = self.context.get('viewer')
        if not viewer or not viewer.is_authenticated:
            return False
        return obj.sender_id == viewer.id


# ─────────────────────────────────────────────────────────────────────────────
# Rooms
# ─────────────────────────────────────────────────────────────────────────────

class ChatRoomSerializer(serializers.ModelSerializer):
    title = serializers.SerializerMethodField()
    avatar_url = serializers.SerializerMethodField()
    initials = serializers.SerializerMethodField()
    other_user = serializers.SerializerMethodField()
    unread = serializers.SerializerMethodField()
    last_message = serializers.SerializerMethodField()
    member_count = serializers.SerializerMethodField()
    members = serializers.SerializerMethodField()

    class Meta:
        model = ChatRoom
        fields = [
            'id', 'name', 'room_type', 'title',
            'avatar_url', 'initials', 'other_user',
            'is_archived', 'is_readonly',
            'unread', 'last_message', 'member_count',
            'members',  # ← add
            'created_at', 'updated_at',
        ]

    def get_members(self, obj):
        return UserMiniSerializer(obj.members.all(), many=True, context=self.context).data

    # ───── computed ─────

    def _other(self, room):
        viewer = self.context.get('viewer')
        if room.room_type != 'direct' or not viewer:
            return None
        return room.members.exclude(id=viewer.id).first()

    def get_title(self, obj):
        if obj.room_type == 'direct':
            other = self._other(obj)
            return _safe_full_name(other) if other else (obj.name or 'Chat')
        return obj.name or '#channel'

    def get_avatar_url(self, obj):
        request = self.context.get('request')
        if obj.room_type == 'direct':
            return _avatar_url(self._other(obj), request=request)
        try:
            if obj.avatar:
                url = obj.avatar.url
                if request is not None:
                    return request.build_absolute_uri(url)
                return url
        except Exception:
            pass
        return ''

    def get_initials(self, obj):
        if obj.room_type == 'direct':
            return _safe_initials(self._other(obj))
        name = obj.name or '#'
        parts = [p for p in name.split() if p]
        return ''.join(p[0].upper() for p in parts[:2]) or '#'

    def get_other_user(self, obj):
        if obj.room_type != 'direct':
            return None
        other = self._other(obj)
        if not other:
            return None
        return UserMiniSerializer(other, context=self.context).data

    def get_unread(self, obj):
        viewer = self.context.get('viewer')
        if not viewer:
            return 0
        try:
            membership = ChatRoomMember.objects.get(room=obj, user=viewer)
            last_read = membership.last_read
        except ChatRoomMember.DoesNotExist:
            last_read = None
        qs = obj.messages.filter(is_deleted=False).exclude(sender=viewer)
        if last_read:
            qs = qs.filter(created_at__gt=last_read)
        return qs.count()

    def get_last_message(self, obj):
        msg = obj.messages.filter(is_deleted=False).order_by('-created_at').first()
        if not msg:
            return None
        if msg.message_type == 'file':
            preview = '📎 File'
        elif msg.message_type == 'image':
            preview = '🖼 Image'
        elif msg.message_type == 'poll':
            preview = '📊 Poll'
        else:
            preview = (msg.content or '')[:80]
        return {
            'id': str(msg.id),
            'sender_name': _safe_full_name(msg.sender),
            'preview': preview,
            'created_at': timezone.localtime(msg.created_at).isoformat(),
            'message_type': msg.message_type,
        }

    def get_member_count(self, obj):
        return obj.members.count()


# ─────────────────────────────────────────────────────────────────────────────
# Device tokens (push)
# ─────────────────────────────────────────────────────────────────────────────

class DeviceTokenSerializer(serializers.Serializer):
    platform = serializers.ChoiceField(choices=['ios', 'android'])
    token = serializers.CharField(max_length=512)
    app_version = serializers.CharField(max_length=32, required=False, allow_blank=True)
