import json
import os
from django.contrib.auth.mixins import LoginRequiredMixin
from django.views.generic import TemplateView, View
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages as django_messages
from django.db.models import Q
from django.http import HttpResponseForbidden, JsonResponse
from django.utils import timezone
from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from spellchecker import SpellChecker

from django.core.files.base import ContentFile

# ---------------------------------------------------------------------------
# Encryption helpers (at-rest + transit)
# ---------------------------------------------------------------------------
# At-rest:  every ChatMessage.content is encrypted via EncryptedContentMixin
#           (see models_mixin.py).  The post_init signal means all ORM-loaded
#           instances already carry plain text by the time they reach this
#           file.  The two places below that reconstruct content from raw DB
#           values (reply previews, forwarded text) explicitly decrypt just
#           in case those values were fetched without the signal firing.
#
# In-transit: enforce TLS at the infrastructure level (nginx / load-balancer).
#             Add to settings.py:
#
#   SESSION_COOKIE_SECURE  = True
#   CSRF_COOKIE_SECURE     = True
#   SECURE_SSL_REDIRECT    = True          # redirect HTTP → HTTPS
#   SECURE_HSTS_SECONDS    = 31536000
#   SECURE_HSTS_INCLUDE_SUBDOMAINS = True
#   SECURE_HSTS_PRELOAD    = True
#
# Django Channels (WebSocket) traffic must be served over WSS (wss://).
# ---------------------------------------------------------------------------
from apps.messaging.encryption import decrypt_content

from apps.messaging.models import (
    ChatRoom, ChatRoomMember, ChatMessage, ChatRoomFile,
    ChatPoll, ChatPollOption, ChatPollVote, ChatMessageMention,
)
from apps.core.models import User
from apps.projects.models import Project, TrackingSheet, TrackingRow, TrackingSheetAccess
from apps.tasks.models import Task
from apps.files.models import SharedFile, FileShareAccess

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _sidebar_rooms(user):
    """
    Return rooms for the sidebar with unread counts and last message.
    """
    all_rooms = (
        ChatRoom.objects.filter(members=user, is_archived=False)
        .select_related('created_by', 'project')
        .prefetch_related('chatroommember_set')
        .order_by('-updated_at')
    )

    rooms_with_meta = []
    for room in all_rooms:
        try:
            membership = room.chatroommember_set.get(user=user)
            last_read = membership.last_read
        except ChatRoomMember.DoesNotExist:
            last_read = None

        unread_qs = room.messages.filter(is_deleted=False).exclude(sender=user)
        if last_read:
            unread_qs = unread_qs.filter(created_at__gt=last_read)

        last_msg = (
            room.messages.filter(is_deleted=False)
            .select_related('sender', 'reply_to', 'reply_to__sender', 'linked_file', 'poll')
            .order_by('-created_at')
            .first()
        )

        rooms_with_meta.append({
            'room': room,
            'unread': unread_qs.count(),
            'last_message': last_msg,
        })
    return rooms_with_meta


def _get_project_manager(project):
    """
    Safely find the project manager-like field across possible project schemas.
    """
    for attr in ['project_manager', 'manager', 'created_by', 'owner', 'team_lead']:
        try:
            value = getattr(project, attr, None)
            if value:
                return value
        except Exception:
            pass
    return None


def _user_can_link_project_room(user, project):
    """
    Determines whether a user can manage/link a project room.
    """
    if not user or not getattr(user, 'is_authenticated', False):
        return False

    if user.is_superuser:
        return True

    pm = _get_project_manager(project)
    if pm and getattr(pm, 'id', None) == user.id:
        return True

    try:
        if hasattr(project, 'team_members') and project.team_members.filter(id=user.id).exists():
            return True
    except Exception:
        pass

    return False


def _can_manage_room(user, room):
    """
    Room managers:
    - superuser
    - room creator
    - project manager/team members for project rooms
    """
    if not user or not getattr(user, 'is_authenticated', False):
        return False

    if user.is_superuser:
        return True

    if room.created_by_id == user.id:
        return True

    if room.room_type == ChatRoom.RoomType.PROJECT and room.project_id:
        return _user_can_link_project_room(user, room.project)

    return False

def _room_project_or_403(room):
    """
    Return the project linked to a room, or raise PermissionError if the room
    is not a valid project room.

    Used by chat command actions like:
    - create_tracker
    - add_tracker_row
    - grant_tracker_access
    """
    if not room:
        raise PermissionError('Room not found.')

    if room.room_type != ChatRoom.RoomType.PROJECT:
        raise PermissionError('This action is only available in project rooms.')

    if not room.project_id or not getattr(room, 'project', None):
        raise PermissionError('This project room is not linked to a project.')

    return room.project

def _can_post_in_room(user, room):
    """
    Posting is blocked for readonly rooms and for non-members.
    """
    if not user or not getattr(user, 'is_authenticated', False):
        return False
    if room.is_readonly:
        return False
    return room.members.filter(id=user.id).exists()


def _safe_full_name(user):
    if not user:
        return 'System'
    try:
        return user.full_name or user.get_full_name() or user.username
    except Exception:
        return str(user)


def _safe_initials(user):
    if not user:
        return 'SY'
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


def _room_group_name(room_id):
    return f'chat_{room_id}'


def _get_msg_file_url(msg):
    if getattr(msg, 'file', None):
        try:
            return msg.file.url
        except Exception:
            pass

    if getattr(msg, 'linked_file_id', None):
        try:
            lf = msg.linked_file
            if lf and lf.file:
                return lf.file.url
        except Exception:
            pass

    return ''


def _get_msg_file_name(msg):
    if getattr(msg, 'file_name', None):
        return msg.file_name

    if getattr(msg, 'linked_file_id', None):
        try:
            lf = msg.linked_file
            if lf:
                return lf.name
        except Exception:
            pass

    return ''


def _get_msg_file_size(msg):
    if getattr(msg, 'file_size', None):
        return msg.file_size

    if getattr(msg, 'linked_file_id', None):
        try:
            lf = msg.linked_file
            if lf:
                return lf.file_size
        except Exception:
            pass

    return 0


def _reaction_summary(reactions):
    """
    Convert stored reactions to {emoji: count}.
    Stored data may be:
    - {'👍': ['user1', 'user2']}
    - {'👍': 2}
    """
    out = {}
    for emoji, users in (reactions or {}).items():
        if isinstance(users, list):
            out[emoji] = len(users)
        elif isinstance(users, int):
            out[emoji] = users
        else:
            out[emoji] = 0
    return out


def _toggle_reaction_on_message(message, user, emoji):
    """
    Toggle reaction membership for a user and return both raw and summary.
    """
    reactions = message.reactions or {}
    user_id = str(user.id)

    emoji_users = reactions.get(emoji, [])
    if not isinstance(emoji_users, list):
        emoji_users = []

    if user_id in emoji_users:
        emoji_users.remove(user_id)
        if emoji_users:
            reactions[emoji] = emoji_users
        else:
            reactions.pop(emoji, None)
    else:
        emoji_users.append(user_id)
        reactions[emoji] = emoji_users

    message.reactions = reactions
    message.save(update_fields=['reactions'])

    return reactions, _reaction_summary(reactions)


def _serialize_poll_for_user(poll, user):
    """
    Full advanced poll serializer for chat template + JS.
    """
    total_votes = poll.votes.count()

    selected_ids = set(
        poll.votes.filter(user=user).values_list('option_id', flat=True)
    ) if user and getattr(user, 'is_authenticated', False) else set()

    options = []
    for opt in poll.options.all():
        count = opt.votes.count()
        percent = round((count / total_votes) * 100, 1) if total_votes else 0
        options.append({
            'id': str(opt.id),
            'text': opt.text,
            'votes': count,
            'percent': percent,
            'is_selected': opt.id in selected_ids,
        })

    is_closed = False
    try:
        is_closed = bool(poll.is_effectively_closed)
    except Exception:
        is_closed = bool(getattr(poll, 'is_closed', False))

    ends_at = getattr(poll, 'ends_at', None)

    return {
        'id': str(poll.id),
        'question': poll.question,
        'allow_multiple': getattr(poll, 'allow_multiple', False),
        'is_anonymous': getattr(poll, 'is_anonymous', False),
        'allow_vote_change': getattr(poll, 'allow_vote_change', True),
        'is_closed': is_closed,
        'ends_at': ends_at.isoformat() if ends_at else None,
        'ends_at_display': timezone.localtime(ends_at).strftime('%b %d, %Y %H:%M') if ends_at else '',
        'total_votes': total_votes,
        'can_close': bool(
            user and getattr(user, 'is_authenticated', False) and (
                getattr(poll, 'created_by_id', None) == user.id or user.is_superuser
            )
        ),
        'options': options,
    }


def _serialize_chat_message(msg, viewer=None):
    """
    Serialize any chat message for realtime and AJAX polling.
    """
    file_url = _get_msg_file_url(msg)
    file_name = _get_msg_file_name(msg)
    file_size = _get_msg_file_size(msg)

    file_size_display = ''
    if file_size:
        try:
            if getattr(msg, 'linked_file_id', None) and getattr(msg, 'linked_file', None):
                file_size_display = getattr(msg.linked_file, 'size_display', '') or str(file_size)
            else:
                file_size_display = str(file_size)
        except Exception:
            file_size_display = str(file_size)

    payload = {
        'type': 'chat_message',
        'message_id': str(msg.id),
        'id': str(msg.id),
        'room_id': str(msg.room_id),
        'sender_id': str(msg.sender_id) if msg.sender_id else '',
        'sender_name': _safe_full_name(msg.sender),
        'sender_initials': _safe_initials(msg.sender),
        'message': msg.content or '',
        'content': msg.content or '',
        'message_type': msg.message_type,
        'timestamp': timezone.localtime(msg.created_at).strftime('%H:%M'),
        'created_at': timezone.localtime(msg.created_at).isoformat(),
        'reply_to_id': str(msg.reply_to_id) if msg.reply_to_id else None,
        'reply_to_sender': (
            _safe_full_name(msg.reply_to.sender)
            if msg.reply_to_id and msg.reply_to and msg.reply_to.sender else ''
        ),
        'reply_to_content': (
            decrypt_content(msg.reply_to.content)[:60]
            if msg.reply_to_id and msg.reply_to and msg.reply_to.content else ''
        ),
        'file_url': file_url,
        'file_name': file_name,
        'file_size': file_size_display,
        'is_edited': getattr(msg, 'is_edited', False),
        'reactions': _reaction_summary(getattr(msg, 'reactions', {})),
    }

    if getattr(msg, 'poll_id', None) and getattr(msg, 'poll', None) and viewer is not None:
        payload['poll'] = _serialize_poll_for_user(msg.poll, viewer)

    if msg.message_type == 'command':
        payload['command_payload'] = getattr(msg, 'command_payload', {}) or {}

    return payload


def _broadcast_chat_message(msg, viewer=None):
    """
    Broadcast a new chat message to the room.
    """
    try:
        channel_layer = get_channel_layer()
        if channel_layer is None:
            return

        payload = _serialize_chat_message(msg, viewer=viewer or msg.sender)

        async_to_sync(channel_layer.group_send)(
            _room_group_name(msg.room_id),
            {
                'type': 'chat.message',
                'payload': payload,
            },
        )
    except Exception:
        pass


def _broadcast_presence_update(user_id):
    """
    Push the user's current effective status to all their DM rooms via WS.
    This means the other person's badge updates instantly on heartbeat.
    """
    try:
        channel_layer = get_channel_layer()
        if channel_layer is None:
            return

        status = _user_effective_status(user_id)
        last_seen = _get_presence_cache(user_id)
        last_seen_display = _format_last_seen(last_seen) if last_seen else ''

        # Only push to direct message rooms
        rooms = ChatRoom.objects.filter(
            members__id=user_id,
            room_type='direct',
            is_archived=False,
        ).values_list('id', flat=True)

        for room_id in rooms:
            try:
                async_to_sync(channel_layer.group_send)(
                    _room_group_name(room_id),
                    {
                        'type': 'chat.typing',   # reuse existing event channel
                        'payload': {
                            'type': 'presence_update',
                            'user_id': str(user_id),
                            'status': status,
                            'last_seen_display': last_seen_display,
                        },
                    },
                )
            except Exception:
                pass
    except Exception:
        pass


def _broadcast_reaction(room_id, message_id, summary):
    """
    Broadcast reaction summary update.
    """
    try:
        channel_layer = get_channel_layer()
        if channel_layer is None:
            return

        async_to_sync(channel_layer.group_send)(
            _room_group_name(room_id),
            {
                'type': 'chat.reaction',
                'payload': {
                    'type': 'chat_reaction',
                    'room_id': str(room_id),
                    'message_id': str(message_id),
                    'reactions': summary,
                },
            },
        )
    except Exception:
        pass


def _broadcast_typing(room_id, sender):
    """
    Broadcast typing state.
    """
    try:
        channel_layer = get_channel_layer()
        if channel_layer is None:
            return

        async_to_sync(channel_layer.group_send)(
            _room_group_name(room_id),
            {
                'type': 'chat.typing',
                'payload': {
                    'type': 'typing',
                    'room_id': str(room_id),
                    'sender_id': str(sender.id),
                    'sender_name': _safe_full_name(sender),
                },
            }
        )
    except Exception:
        pass


def _broadcast_poll_update(poll, actor=None):
    """
    Broadcast a full advanced poll payload so the chat UI can redraw compactly.
    """
    try:
        channel_layer = get_channel_layer()
        if channel_layer is None:
            return

        actor = actor or poll.created_by or poll.message.sender

        data = {
            'type': 'poll_update',
            'poll_id': str(poll.id),
            'poll': _serialize_poll_for_user(poll, actor),
        }

        async_to_sync(channel_layer.group_send)(
            _room_group_name(poll.message.room_id),
            {
                'type': 'chat.poll',
                'payload': data,
            }
        )
    except Exception:
        pass


def _combine_forwarded_text(source_msg, note=''):
    original_sender = _safe_full_name(source_msg.sender)
    original_text = decrypt_content(source_msg.content or '').strip()
    note = (note or '').strip()
    header = f"Forwarded from {original_sender}"

    if original_text and note:
        return f"{note}\n\n{header}:\n{original_text}"
    if original_text:
        return f"{header}:\n{original_text}"
    if note:
        return f"{note}\n\n{header}"
    return header


def _copy_chat_file_to_contentfile(msg):
    """
    Copy a chat attachment or linked file into a ContentFile.
    Returns: (src_name, content_file, byte_length, mime_type)
    """
    from django.core.files.base import ContentFile

    src_name = _get_msg_file_name(msg) or 'attachment'
    mime_type = 'application/octet-stream'
    file_bytes = b''

    if getattr(msg, 'linked_file_id', None) and getattr(msg, 'linked_file', None):
        lf = msg.linked_file
        src_name = lf.name or src_name
        mime_type = getattr(lf, 'file_type', None) or mime_type
        lf.file.open('rb')
        try:
            file_bytes = lf.file.read()
        finally:
            try:
                lf.file.close()
            except Exception:
                pass

    elif getattr(msg, 'file', None):
        try:
            msg.file.open('rb')
            file_bytes = msg.file.read()
        finally:
            try:
                msg.file.close()
            except Exception:
                pass

    if not file_bytes:
        return None, None, 0, mime_type

    return src_name, ContentFile(file_bytes), len(file_bytes), mime_type


def _parse_mentions_from_text(room, text):
    """
    Resolve @mentions against room members.
    Matches against username, first_name, or last_name.
    """
    import re

    if not text:
        return []

    tokens = re.findall(r'@([A-Za-z0-9._-]+)', text)
    if not tokens:
        return []

    members = room.members.filter(
        Q(username__in=tokens) |
        Q(first_name__in=tokens) |
        Q(last_name__in=tokens)
    ).distinct()

    return list(members)


def _save_mentions(message):
    """
    Save mention rows for a message.
    """
    mentioned_users = _parse_mentions_from_text(message.room, message.content or '')
    for user in mentioned_users:
        ChatMessageMention.objects.get_or_create(message=message, user=user)


def _tracking_permission(user, sheet):
    """
    Permission resolution for tracking sheets.
    """
    if not user or not getattr(user, 'is_authenticated', False):
        return None

    if user.is_superuser:
        return 'full'

    if sheet.project and getattr(sheet.project, 'project_manager_id', None) == user.id:
        return 'full'

    direct = sheet.access_rows.filter(user=user).first()
    if direct:
        return direct.permission

    try:
        if sheet.project.team_members.filter(id=user.id).exists():
            return 'input'
    except Exception:
        pass

    return None


def _get_project_chat_files_for_user(user, project, room=None):
    """
    Returns project files visible to the user PLUS files explicitly pinned
    to the room, even if they are not normally visible through standard file
    visibility rules.
    """
    from apps.files.models import SharedFile

    profile = getattr(user, 'staffprofile', None)
    unit = profile.unit if profile else None
    dept = profile.department if profile else None

    own_ids = set(
        SharedFile.objects.filter(project=project, is_latest=True).filter(
            Q(uploaded_by=user) |
            Q(visibility='office') |
            Q(visibility='unit', unit=unit) |
            Q(visibility='department', department=dept) |
            Q(shared_with=user) |
            Q(share_access__user=user) |
            Q(project__project_manager=user) |
            Q(project__team_members=user) |
            Q(project__is_public=True)
        ).distinct().values_list('id', flat=True)
    )

    room_pinned_ids = set()
    if room:
        room_pinned_ids = set(
            ChatRoomFile.objects.filter(room=room).values_list('file_id', flat=True)
        )

    combined_ids = own_ids | room_pinned_ids
    if not combined_ids:
        return []

    qs = (
        SharedFile.objects.filter(id__in=combined_ids, is_latest=True)
        .select_related('folder')
        .order_by('-created_at')
    )

    files = []
    for f in qs[:100]:
        try:
            file_url = f.file.url
        except Exception:
            continue

        files.append({
            'id': str(f.id),
            'name': f.name,
            'url': file_url,
            'size': getattr(f, 'size_display', '') or '',
            'is_image': getattr(f, 'is_image', False),
            'icon_class': getattr(f, 'icon_class', 'bi bi-file-earmark'),
            'icon_color': getattr(f, 'icon_color', '#6b7280'),
            'folder': f.folder.name if getattr(f, 'folder', None) else '',
            'created_at': f.created_at,
            'room_pinned': f.id in room_pinned_ids,
        })

    return files


def _managed_projects_for_user(user):
    """
    Projects the user can manage from messaging.
    """
    if user.is_superuser:
        return Project.objects.all().order_by('name')

    return Project.objects.filter(
        Q(project_manager=user) | Q(team_members=user)
    ).distinct().order_by('name')


spell = SpellChecker()

def _spellcheck_text(text):
    words = text.split()
    misspelled = spell.unknown(words)

    suggestions = []

    for word in misspelled:
        suggestions.append({
            'word': word,
            'suggestions': list(spell.candidates(word))[:3]
        })

    corrected = " ".join(
        spell.correction(w) if w in misspelled else w
        for w in words
    )

    return {
        'corrected': corrected,
        'suggestions': suggestions
    }


def _annotate_is_online(user_qs_or_list):
    """
    Stamp a fresh `is_online` attribute on each user by reading the same
    Redis presence cache the heartbeat writes to. One attribute per user,
    no DB hit, no N+1.

    Use this anywhere you render a list of users and need `staff.is_online`
    to reflect actual browser presence (not just `is_authenticated`, which
    stays True forever after login).
    """
    users = list(user_qs_or_list)  # materialise once
    for u in users:
        try:
            u.is_online = _user_is_online(u.pk)
        except Exception:
            u.is_online = False
    return users
# ---------------------------------------------------------------------------
# Chat List
# ---------------------------------------------------------------------------

class ChatListView(LoginRequiredMixin, TemplateView):
    template_name = 'messaging/chat_list.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['rooms_meta'] = _sidebar_rooms(self.request.user)

        # 🩹 FIX: annotate is_online from Redis presence cache so the
        # template's `{% if staff.is_online %}` reflects real presence.
        staff_qs = (
            User.objects.filter(status='active', is_active=True)
            .exclude(id=self.request.user.id)
            .select_related('staffprofile', 'staffprofile__position', 'staffprofile__department')
            .order_by('first_name')
        )
        ctx['all_staff'] = _annotate_is_online(staff_qs)

        ctx['room_types'] = ChatRoom.RoomType.choices
        ctx['managed_projects'] = _managed_projects_for_user(self.request.user)
        return ctx


# ---------------------------------------------------------------------------
# Chat Room
# ---------------------------------------------------------------------------

class ChatRoomView(LoginRequiredMixin, View):
    template_name = 'messaging/chat_room.html'

    def get(self, request, room_id):
        room = get_object_or_404(
            ChatRoom.objects.select_related('project'),
            id=room_id,
            members=request.user,
        )

        chat_messages = (
            room.messages.filter(is_deleted=False)
            .select_related('sender', 'reply_to', 'reply_to__sender', 'linked_file')
            .prefetch_related('poll__options', 'poll__options__votes')
            .order_by('created_at')
        )

        members = (
            room.members.filter(is_active=True)
            .select_related('staffprofile', 'staffprofile__position')
            .order_by('first_name', 'last_name')
        )

        ChatRoomMember.objects.filter(room=room, user=request.user).update(
            last_read=timezone.now()
        )

        existing_member_ids = list(room.members.values_list('id', flat=True))

        project_files    = []
        can_link_project = False

        if room.project_id:
            project_files    = _get_project_chat_files_for_user(request.user, room.project, room=room)
            can_link_project = _user_can_link_project_room(request.user, room.project)

        ctx = {
            'room':             room,
            'chat_messages':    chat_messages,
            'members':          members,
            'rooms_meta':       _sidebar_rooms(request.user),
            'all_staff': _annotate_is_online(
                User.objects.filter(status='active', is_active=True)
                .exclude(id__in=existing_member_ids)
                .order_by('first_name')
            ),
            'room_types':       ChatRoom.RoomType.choices,
            'can_manage_room':  _can_manage_room(request.user, room),
            'project_files':    project_files,
            'can_link_project': can_link_project,
            'managed_projects': _managed_projects_for_user(request.user),
        }
        return render(request, self.template_name, ctx)

    def post(self, request, room_id):
        room = get_object_or_404(ChatRoom, id=room_id, members=request.user)
        is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'

        if not _can_post_in_room(request.user, room):
            if is_ajax:
                return JsonResponse({'ok': False, 'error': 'This room is read-only.'}, status=403)
            django_messages.warning(request, 'This room is read-only.')
            return redirect('chat_room', room_id=room_id)

        content     = request.POST.get('content', '').strip()
        reply_to_id = request.POST.get('reply_to')
        reply_to    = None

        if reply_to_id:
            try:
                reply_to = ChatMessage.objects.select_related('sender').get(id=reply_to_id, room=room)
            except ChatMessage.DoesNotExist:
                reply_to = None

        new_message = None

        if 'file' in request.FILES:
            f        = request.FILES['file']
            msg_type = 'image' if (getattr(f, 'content_type', '') or '').startswith('image/') else 'file'
            new_message = ChatMessage.objects.create(
                room=room, sender=request.user, message_type=msg_type,
                file=f, file_name=f.name, file_size=f.size,
                content=content, reply_to=reply_to,
            )
        elif content:
            new_message = ChatMessage.objects.create(
                room=room, sender=request.user, content=content,
                message_type='text', reply_to=reply_to,
            )

        if new_message:
            room.updated_at = timezone.now()
            room.save(update_fields=['updated_at'])
            ChatRoomMember.objects.filter(room=room, user=request.user).update(last_read=timezone.now())
            _broadcast_chat_message(new_message)
            # Notify offline/away members via email
            _notify_offline_members(room, request.user, new_message)

        # Fetch/AJAX callers get JSON back — no page reload
        if is_ajax:
            payload = _serialize_chat_message(new_message, viewer=request.user) if new_message else None
            return JsonResponse({'ok': True, 'message': payload})

        return redirect('chat_room', room_id=room_id)


# ---------------------------------------------------------------------------
# Direct Message
# ---------------------------------------------------------------------------

class DirectMessageView(LoginRequiredMixin, View):
    def get(self, request, user_id):
        target   = get_object_or_404(User, id=user_id)
        existing = (
            ChatRoom.objects.filter(room_type='direct', members=request.user)
            .filter(members=target)
            .first()
        )
        if existing:
            return redirect('chat_room', room_id=existing.id)

        room = ChatRoom.objects.create(
            name=f'{_safe_full_name(request.user)} & {_safe_full_name(target)}',
            room_type='direct',
            created_by=request.user,
        )
        ChatRoomMember.objects.create(room=room, user=request.user, role='admin')
        ChatRoomMember.objects.create(room=room, user=target,       role='admin')
        return redirect('chat_room', room_id=room.id)


# ---------------------------------------------------------------------------
# Create Chat Room
# ---------------------------------------------------------------------------

class CreateChatRoomView(LoginRequiredMixin, View):
    def get(self, request):
        return redirect('chat_list')

    def post(self, request):
        name        = request.POST.get('name', '').strip()
        room_type   = request.POST.get('room_type', 'group')
        description = request.POST.get('description', '').strip()
        project_id  = request.POST.get('project_id', '').strip()

        if not name:
            django_messages.error(request, 'Room name is required.')
            return redirect('chat_list')

        linked_project = None

        if room_type == ChatRoom.RoomType.PROJECT:
            if not project_id:
                django_messages.error(request, 'Please select a project for a project chat.')
                return redirect('chat_list')

            linked_project = get_object_or_404(Project, id=project_id)

            if not _user_can_link_project_room(request.user, linked_project):
                return HttpResponseForbidden('Only the project manager can create a project-linked chat.')

            existing_room = ChatRoom.objects.filter(
                room_type=ChatRoom.RoomType.PROJECT,
                project=linked_project,
                is_archived=False,
            ).first()

            if existing_room:
                django_messages.info(request, f'A chat room for "{linked_project}" already exists.')
                return redirect('chat_room', room_id=existing_room.id)

            if not name:
                name = str(linked_project)

        room = ChatRoom.objects.create(
            name=name, room_type=room_type, created_by=request.user,
            description=description, project=linked_project,
        )
        ChatRoomMember.objects.create(room=room, user=request.user, role='admin')

        added_user_ids = set()
        for uid in request.POST.getlist('members'):
            try:
                u = User.objects.get(id=uid, is_active=True, status='active')
            except User.DoesNotExist:
                continue
            ChatRoomMember.objects.get_or_create(room=room, user=u, defaults={'role': 'member'})
            added_user_ids.add(str(u.id))

        if linked_project:
            pm = _get_project_manager(linked_project)
            if pm and str(pm.id) not in added_user_ids and pm.id != request.user.id:
                ChatRoomMember.objects.get_or_create(room=room, user=pm, defaults={'role': 'admin'})

        django_messages.success(request, f'"{room.name}" created.')
        return redirect('chat_room', room_id=room.id)


# ---------------------------------------------------------------------------
# Add / Remove Room Member
# ---------------------------------------------------------------------------

class AddRoomMemberView(LoginRequiredMixin, View):
    def post(self, request, room_id):
        room = get_object_or_404(ChatRoom, id=room_id, members=request.user)

        if not _can_manage_room(request.user, room):
            return HttpResponseForbidden('Only the room creator can add members.')
        if room.room_type == ChatRoom.RoomType.DIRECT:
            django_messages.error(request, 'Direct messages cannot have extra members.')
            return redirect('chat_room', room_id=room.id)

        added_count = 0
        for uid in request.POST.getlist('members'):
            try:
                user = User.objects.get(id=uid, is_active=True, status='active')
            except User.DoesNotExist:
                continue
            _, created = ChatRoomMember.objects.get_or_create(
                room=room, user=user, defaults={'role': 'member'}
            )
            if created:
                added_count += 1

        if added_count:
            django_messages.success(request, f'{added_count} member(s) added to "{room.name}".')
        else:
            django_messages.info(request, 'No new members were added.')

        return redirect('chat_room', room_id=room.id)


class RemoveRoomMemberView(LoginRequiredMixin, View):
    def post(self, request, room_id, user_id):
        room   = get_object_or_404(ChatRoom, id=room_id, members=request.user)
        member = get_object_or_404(User, id=user_id)

        if not _can_manage_room(request.user, room):
            return HttpResponseForbidden('Only the room creator can remove members.')
        if room.room_type == ChatRoom.RoomType.DIRECT:
            django_messages.error(request, 'Direct message participants cannot be removed.')
            return redirect('chat_room', room_id=room.id)
        if member.id == room.created_by_id:
            django_messages.error(request, 'The room creator cannot be removed.')
            return redirect('chat_room', room_id=room.id)

        deleted, _ = ChatRoomMember.objects.filter(room=room, user=member).delete()

        if deleted:
            django_messages.success(request, f'{_safe_full_name(member)} removed from "{room.name}".')
        else:
            django_messages.info(request, 'Member was not found in this room.')

        return redirect('chat_room', room_id=room.id)


# ---------------------------------------------------------------------------
# Delete Room
# ---------------------------------------------------------------------------

class DeleteRoomView(LoginRequiredMixin, View):
    def post(self, request, room_id):
        room = get_object_or_404(ChatRoom, id=room_id, members=request.user)
        if not _can_manage_room(request.user, room):
            return HttpResponseForbidden('Only the room creator can delete this channel.')
        room_name = room.name
        room.delete()
        django_messages.success(request, f'"{room_name}" deleted successfully.')
        return redirect('chat_list')


# ---------------------------------------------------------------------------
# Typing ping (HTTP fallback)
# ---------------------------------------------------------------------------

class ChatTypingPingView(LoginRequiredMixin, View):
    def post(self, request, room_id):
        room = get_object_or_404(ChatRoom, id=room_id, members=request.user)
        if not _can_post_in_room(request.user, room):
            return HttpResponseForbidden('Cannot type in this room.')
        _broadcast_typing(room.id, request.user)
        return JsonResponse({'status': 'ok'})


# ---------------------------------------------------------------------------
# Message poll (new messages — WS fallback)
# ---------------------------------------------------------------------------

class ChatMessagesPollView(LoginRequiredMixin, View):
    def get(self, request, room_id):
        room     = get_object_or_404(ChatRoom, id=room_id, members=request.user)
        after_id = request.GET.get('after_id', '').strip()

        qs = (
            room.messages.filter(is_deleted=False)
            .select_related('sender', 'reply_to', 'reply_to__sender', 'linked_file')
            .order_by('created_at')
        )

        if after_id:
            try:
                anchor = room.messages.get(id=after_id)
                qs     = qs.filter(created_at__gt=anchor.created_at)
            except ChatMessage.DoesNotExist:
                pass

        messages_data = [_serialize_chat_message(msg) for msg in qs]
        ChatRoomMember.objects.filter(room=room, user=request.user).update(last_read=timezone.now())
        return JsonResponse({'ok': True, 'messages': messages_data})


# ---------------------------------------------------------------------------
# Reactions poll
# ---------------------------------------------------------------------------

class ChatReactionsPollView(LoginRequiredMixin, View):
    def get(self, request, room_id):
        room = get_object_or_404(ChatRoom, id=room_id, members=request.user)
        qs = (
            room.messages
            .filter(is_deleted=False)
            .exclude(reactions={})
            .order_by('-created_at')
            .values('id', 'reactions')
            [:200]
        )
        data = {}
        for row in qs:
            summary = _reaction_summary(row['reactions'])
            if summary:
                data[str(row['id'])] = summary
        return JsonResponse({'ok': True, 'reactions': data})


# ---------------------------------------------------------------------------
# Edits poll  (HTTP fallback for receivers who miss the WS broadcast)
# ---------------------------------------------------------------------------

class ChatEditsPollView(LoginRequiredMixin, View):
    """
    GET /messages/<room_id>/edits-poll/?since=<iso-timestamp>

    Returns all edited messages in the room whose edited_at is after
    the given timestamp.  The JS polls this every 3 s alongside the
    reactions poll so receivers who are on the HTTP fallback (no WS)
    still see edits applied without a manual refresh.
    """
    def get(self, request, room_id):
        from datetime import datetime
        room = get_object_or_404(ChatRoom, id=room_id, members=request.user)

        since_str = request.GET.get('since', '').strip()
        since_dt  = None
        if since_str:
            try:
                since_dt = datetime.fromisoformat(since_str)
                if timezone.is_naive(since_dt):
                    since_dt = timezone.make_aware(since_dt)
            except Exception:
                since_dt = None

        qs = room.messages.filter(
            is_deleted=False,
            is_edited=True,
        ).exclude(edited_at__isnull=True)

        if since_dt:
            qs = qs.filter(edited_at__gt=since_dt)

        qs = qs.select_related('sender').order_by('edited_at')[:50]

        data = []
        for msg in qs:
            data.append({
                'message_id': str(msg.id),
                'content':    msg.content or '',
                'edited_at':  timezone.localtime(msg.edited_at).isoformat(),
            })

        return JsonResponse({'ok': True, 'edits': data})


# ---------------------------------------------------------------------------
# Toggle Reaction
# ---------------------------------------------------------------------------

class ToggleReactionView(LoginRequiredMixin, View):
    def post(self, request, room_id, message_id):
        room    = get_object_or_404(ChatRoom,    id=room_id,    members=request.user)
        message = get_object_or_404(ChatMessage, id=message_id, room=room, is_deleted=False)
        emoji   = (request.POST.get('emoji') or '').strip()
        if not emoji:
            return JsonResponse({'ok': False, 'error': 'Emoji is required.'}, status=400)
        reactions = _toggle_reaction_on_message(message, request.user, emoji)
        summary   = _reaction_summary(reactions)
        _broadcast_reaction(room.id, message.id, summary)
        return JsonResponse({'ok': True, 'message_id': str(message.id), 'reactions': summary})


# ---------------------------------------------------------------------------
# Delete message (soft delete)
# ---------------------------------------------------------------------------

class DeleteMessageView(LoginRequiredMixin, View):
    def post(self, request, message_id):
        msg = get_object_or_404(
            ChatMessage,
            id=message_id,
            sender=request.user,
            is_deleted=False,
        )
        msg.is_deleted = True
        msg.save(update_fields=['is_deleted'])
        return JsonResponse({'ok': True})


# ---------------------------------------------------------------------------
# Edit Message  (own last message only, within 1 hour of sending)
# ---------------------------------------------------------------------------

class EditMessageView(LoginRequiredMixin, View):
    """
    POST /messages/api/edit/<message_id>/

    Allows a user to edit the content of their own last message in a room,
    provided it was sent within the past hour.

    Rules enforced:
      1. Sender must be request.user.
      2. Message must not be deleted.
      3. Message must be the most-recent non-deleted message from the user
         in that room (i.e. their last message).
      4. Message must have been created within the last 3600 seconds (1 hour).
      5. Only text-type messages can be edited (not files/images/polls).
    """

    EDIT_WINDOW_SECONDS = 3600  # 1 hour

    def post(self, request, message_id):
        msg = get_object_or_404(
            ChatMessage,
            id=message_id,
            sender=request.user,
            is_deleted=False,
        )

        # ── Rule 5: only plain text messages ──────────────────────────────────
        if msg.message_type not in ('text', ''):
            return JsonResponse({'ok': False, 'error': 'Only text messages can be edited.'}, status=400)

        # ── Rule 4: within the 1-hour edit window ────────────────────────────
        age = (timezone.now() - msg.created_at).total_seconds()
        if age > self.EDIT_WINDOW_SECONDS:
            return JsonResponse({'ok': False, 'error': 'Edit window has expired (1 hour).'}, status=403)

        # ── Rule 3: must be the user's last message in this room ─────────────
        last_msg = (
            ChatMessage.objects
            .filter(room=msg.room, sender=request.user, is_deleted=False)
            .order_by('-created_at')
            .first()
        )
        if not last_msg or str(last_msg.id) != str(msg.id):
            return JsonResponse({'ok': False, 'error': 'You can only edit your last message.'}, status=403)

        # ── Apply the edit ────────────────────────────────────────────────────
        new_content = (request.POST.get('content') or '').strip()
        if not new_content:
            return JsonResponse({'ok': False, 'error': 'Message cannot be empty.'}, status=400)

        msg.content   = new_content
        msg.is_edited = True
        msg.edited_at = timezone.now()
        msg.save(update_fields=['content', 'is_edited', 'edited_at'])

        # Broadcast the edit so all connected clients update in real-time
        payload = _serialize_chat_message(msg, viewer=request.user)
        payload['type'] = 'message_edited'

        try:
            channel_layer = get_channel_layer()
            if channel_layer:
                from asgiref.sync import async_to_sync
                async_to_sync(channel_layer.group_send)(
                    _room_group_name(msg.room_id),
                    {'type': 'chat.edit', 'payload': payload},
                )
        except Exception:
            pass

        return JsonResponse({'ok': True, 'message': payload})


# ---------------------------------------------------------------------------
# Files-app picker API
# ---------------------------------------------------------------------------

class ChatFilePickerAPIView(LoginRequiredMixin, View):
    """
    GET /messages/<room_id>/files/?q=<search>
    Returns files the requesting user can access, for the in-chat file picker.
    """
    def get(self, request, room_id):
        from apps.files.models import SharedFile

        get_object_or_404(ChatRoom, id=room_id, members=request.user)

        q       = request.GET.get('q', '').strip()
        profile = getattr(request.user, 'staffprofile', None)
        unit    = profile.unit       if profile else None
        dept    = profile.department if profile else None

        qs = SharedFile.objects.filter(
            Q(uploaded_by=request.user)
            | Q(visibility='office')
            | Q(visibility='unit',       unit=unit)
            | Q(visibility='department', department=dept)
            | Q(shared_with=request.user)
            | Q(share_access__user=request.user)
        ).filter(is_latest=True).distinct()

        if q:
            qs = qs.filter(name__icontains=q)

        qs = qs.select_related('folder').order_by('-created_at')[:120]

        files = []
        for f in qs:
            try:
                url = f.file.url
            except Exception:
                continue
            files.append({
                'id':            str(f.id),
                'name':          f.name,
                'size':          f.size_display,
                'file_size':     f.file_size,
                'is_image':      f.is_image,
                'icon_class':    f.icon_class,
                'icon_color':    f.icon_color,
                'type_category': f.type_category,
                'url':           url,
                'folder':        f.folder.name if f.folder else '',
                'created_at':    f.created_at.strftime('%d %b %Y'),
            })
        return JsonResponse({'files': files})


# ---------------------------------------------------------------------------
# Attach Files-app file to chat message
# ---------------------------------------------------------------------------

class ChatAttachSharedFileView(LoginRequiredMixin, View):
    def post(self, request, room_id):
        from apps.files.models import SharedFile

        room = get_object_or_404(ChatRoom, id=room_id, members=request.user)
        if room.is_readonly:
            return JsonResponse({'error': 'Room is read-only.'}, status=403)

        file_id     = request.POST.get('file_id',  '').strip()
        caption     = request.POST.get('caption',  '').strip()
        reply_to_id = request.POST.get('reply_to', '').strip() or None

        if not file_id:
            return JsonResponse({'error': 'No file_id provided.'}, status=400)

        profile = getattr(request.user, 'staffprofile', None)
        unit    = profile.unit       if profile else None
        dept    = profile.department if profile else None

        shared_file = get_object_or_404(
            SharedFile.objects.filter(
                Q(uploaded_by=request.user)
                | Q(visibility='office')
                | Q(visibility='unit',       unit=unit)
                | Q(visibility='department', department=dept)
                | Q(shared_with=request.user)
                | Q(share_access__user=request.user)
            ).distinct(),
            pk=file_id,
        )

        reply_obj         = None
        reply_sender_name = ''
        reply_content     = ''
        if reply_to_id:
            try:
                reply_obj         = ChatMessage.objects.select_related('sender').get(id=reply_to_id, room=room)
                reply_sender_name = _safe_full_name(reply_obj.sender)
                reply_content     = (reply_obj.content or '')[:80]
            except ChatMessage.DoesNotExist:
                reply_obj = None

        msg_type = 'image' if shared_file.is_image else 'file'
        msg = ChatMessage.objects.create(
            room=room, sender=request.user, content=caption,
            message_type=msg_type, reply_to=reply_obj,
            linked_file=shared_file, file_name=shared_file.name,
            file_size=shared_file.file_size,
        )

        room.updated_at = timezone.now()
        room.save(update_fields=['updated_at'])
        ChatRoomMember.objects.filter(room=room, user=request.user).update(last_read=timezone.now())

        try:
            file_url = shared_file.file.url
        except Exception:
            file_url = ''

        payload = {
            'type':             'chat_message',
            'message_id':       str(msg.id),
            'room_id':          str(room.id),
            'sender_id':        str(request.user.id),
            'sender_name':      _safe_full_name(request.user),
            'sender_initials':  _safe_initials(request.user),
            'message':          caption,
            'content':          caption,
            'message_type':     msg_type,
            'timestamp':        timezone.localtime(msg.created_at).strftime('%H:%M'),
            'created_at':       timezone.localtime(msg.created_at).isoformat(),
            'file_url':         file_url,
            'file_name':        shared_file.name,
            'file_size':        shared_file.size_display,
            'reply_to_id':      str(reply_obj.id) if reply_obj else None,
            'reply_to_sender':  reply_sender_name,
            'reply_to_content': reply_content,
            'reactions':        {},
            'is_edited':        False,
        }

        try:
            channel_layer = get_channel_layer()
            if channel_layer:
                async_to_sync(channel_layer.group_send)(
                    _room_group_name(room_id),
                    {'type': 'chat.message', 'payload': payload},
                )
        except Exception:
            pass

        return JsonResponse({'ok': True, 'payload': payload})


# ---------------------------------------------------------------------------
# Forward message
# ---------------------------------------------------------------------------

class ForwardChatMessageView(LoginRequiredMixin, View):
    def post(self, request, room_id, message_id):
        source_room = get_object_or_404(ChatRoom, id=room_id, members=request.user)
        source_msg  = get_object_or_404(
            ChatMessage.objects.select_related('sender', 'linked_file', 'room'),
            id=message_id, room=source_room, is_deleted=False,
        )

        target_room_id = (request.POST.get('target_room_id') or '').strip()
        note           = (request.POST.get('note') or '').strip()

        if not target_room_id:
            return JsonResponse({'ok': False, 'error': 'Target room is required.'}, status=400)

        target_room = get_object_or_404(ChatRoom, id=target_room_id, members=request.user)
        if target_room.is_readonly:
            return JsonResponse({'ok': False, 'error': 'Target room is read-only.'}, status=403)

        forwarded = None

        if source_msg.linked_file_id:
            forwarded = ChatMessage.objects.create(
                room=target_room, sender=request.user,
                message_type=source_msg.message_type,
                linked_file=source_msg.linked_file,
                file_name=_get_msg_file_name(source_msg),
                file_size=_get_msg_file_size(source_msg),
                content=_combine_forwarded_text(source_msg, note),
            )
        elif source_msg.message_type in ('file', 'image') and source_msg.file:
            file_name, content_file, file_size, _mime = _copy_chat_file_to_contentfile(source_msg)
            if not content_file:
                return JsonResponse({'ok': False, 'error': 'Could not read the source file.'}, status=400)
            forwarded = ChatMessage(
                room=target_room, sender=request.user,
                message_type=source_msg.message_type,
                file_name=file_name, file_size=file_size,
                content=_combine_forwarded_text(source_msg, note),
            )
            forwarded.file.save(os.path.basename(file_name), content_file, save=False)
            forwarded.save()
        else:
            forwarded = ChatMessage.objects.create(
                room=target_room, sender=request.user,
                message_type='text',
                content=_combine_forwarded_text(source_msg, note),
            )

        target_room.updated_at = timezone.now()
        target_room.save(update_fields=['updated_at'])
        ChatRoomMember.objects.filter(room=target_room, user=request.user).update(last_read=timezone.now())
        _broadcast_chat_message(forwarded)

        return JsonResponse({
            'ok': True,
            'message': 'Message forwarded successfully.',
            'payload': _serialize_chat_message(forwarded),
        })


# ---------------------------------------------------------------------------
# Save chat file to Files app
# ---------------------------------------------------------------------------

class SaveChatMessageToFilesView(LoginRequiredMixin, View):
    def post(self, request, room_id, message_id):
        from apps.files.models import SharedFile, FileFolder

        room = get_object_or_404(ChatRoom, id=room_id, members=request.user)
        msg  = get_object_or_404(
            ChatMessage.objects.select_related('sender', 'linked_file'),
            id=message_id, room=room, is_deleted=False,
        )

        if msg.message_type not in ('file', 'image'):
            return JsonResponse({'ok': False, 'error': 'Only file or image messages can be saved.'}, status=400)

        folder    = None
        folder_id = (request.POST.get('folder_id') or '').strip()
        if folder_id:
            folder = get_object_or_404(FileFolder, id=folder_id, owner=request.user)

        file_name, content_file, file_size, mime_type = _copy_chat_file_to_contentfile(msg)
        if not content_file:
            return JsonResponse({'ok': False, 'error': 'Could not read file data.'}, status=400)

        saved = SharedFile.objects.create(
            name=file_name, uploaded_by=request.user, folder=folder,
            visibility='private',
            description=f'Saved from chat room "{room.name}"',
            file_size=file_size, file_type=mime_type,
        )
        saved.file.save(os.path.basename(file_name), content_file, save=True)

        try:
            saved.file_hash = saved.compute_hash()
            saved.save(update_fields=['file_hash'])
        except Exception:
            pass

        return JsonResponse({
            'ok': True,
            'message': f'"{saved.name}" saved to Files successfully.',
            'file_id': str(saved.id),
            'file_name': saved.name,
        })


# ---------------------------------------------------------------------------
# Pin a project file to the chat room
# ---------------------------------------------------------------------------

class ChatRoomFilePinView(LoginRequiredMixin, View):
    """
    POST /messages/<room_id>/files/pin/
    Pins a SharedFile to the room so ALL members can access it,
    bypassing normal file-visibility rules. Only room managers can pin.
    """
    def post(self, request, room_id):
        from apps.files.models import SharedFile

        room = get_object_or_404(ChatRoom, id=room_id, members=request.user)

        if not _can_manage_room(request.user, room):
            return JsonResponse({'ok': False, 'error': 'Only room managers can share files.'}, status=403)

        file_id = request.POST.get('file_id', '').strip()
        note    = request.POST.get('note', '').strip()

        if not file_id:
            return JsonResponse({'ok': False, 'error': 'file_id is required.'}, status=400)

        shared_file = get_object_or_404(SharedFile, pk=file_id)

        obj, created = ChatRoomFile.objects.get_or_create(
            room=room,
            file=shared_file,
            defaults={'shared_by': request.user, 'note': note},
        )

        if created:
            # Post a system message so all members see the file was shared
            sys_msg = ChatMessage.objects.create(
                room=room,
                sender=request.user,
                message_type='system',
                content=(
                    f'📎 {_safe_full_name(request.user)} shared '
                    f'"{shared_file.name}" with this room.'
                ),
            )
            room.updated_at = timezone.now()
            room.save(update_fields=['updated_at'])
            _broadcast_chat_message(sys_msg)

        return JsonResponse({
            'ok':        True,
            'pinned':    created,
            'file_id':   str(shared_file.id),
            'file_name': shared_file.name,
        })


# ---------------------------------------------------------------------------
# Unpin a project file from the chat room
# ---------------------------------------------------------------------------

class ChatRoomFileUnpinView(LoginRequiredMixin, View):
    """
    POST /messages/<room_id>/files/<file_id>/unpin/
    Removes a pinned file from room access. Only room managers.
    """
    def post(self, request, room_id, file_id):
        room = get_object_or_404(ChatRoom, id=room_id, members=request.user)

        if not _can_manage_room(request.user, room):
            return JsonResponse({'ok': False, 'error': 'Only room managers can remove shared files.'}, status=403)

        deleted, _ = ChatRoomFile.objects.filter(room=room, file_id=file_id).delete()
        return JsonResponse({'ok': True, 'removed': deleted > 0})

class CreatePollView(LoginRequiredMixin, View):
    def post(self, request, room_id):
        room = get_object_or_404(ChatRoom, id=room_id, members=request.user)

        if room.is_readonly:
            return JsonResponse({'ok': False, 'error': 'This room is read-only.'}, status=403)

        question = (request.POST.get('question') or '').strip()
        options = [o.strip() for o in request.POST.getlist('options[]') if o.strip()]

        allow_multiple = request.POST.get('allow_multiple') == '1'
        allow_vote_change = request.POST.get('allow_vote_change', '1') == '1'

        if not question:
            return JsonResponse({'ok': False, 'error': 'Question is required.'}, status=400)

        if len(options) < 2:
            return JsonResponse({'ok': False, 'error': 'At least two options are required.'}, status=400)

        msg = ChatMessage.objects.create(
            room=room,
            sender=request.user,
            message_type='poll',
            content=question,
        )

        poll = ChatPoll.objects.create(
            message=msg,
            question=question,
            allow_multiple=allow_multiple,
            allow_vote_change=allow_vote_change,
            created_by=request.user,
        )

        for opt in options:
            ChatPollOption.objects.create(poll=poll, text=opt)

        room.updated_at = timezone.now()
        room.save(update_fields=['updated_at'])

        ChatRoomMember.objects.filter(room=room, user=request.user)\
            .update(last_read=timezone.now())

        payload = _serialize_chat_message(msg, viewer=request.user)
        payload['poll'] = _serialize_poll_for_user(poll, request.user)

        _broadcast_chat_message(msg, viewer=request.user)

        return JsonResponse({'ok': True, 'payload': payload})


class VotePollView(LoginRequiredMixin, View):
    def post(self, request, poll_id):
        poll = get_object_or_404(
            ChatPoll.objects.select_related('message', 'message__room'),
            id=poll_id
        )

        room = poll.message.room

        if not room.members.filter(id=request.user.id).exists():
            return JsonResponse({'ok': False, 'error': 'Not allowed'}, status=403)

        if getattr(poll, 'is_closed', False):
            return JsonResponse({'ok': False, 'error': 'Poll is closed'}, status=400)

        option_id = request.POST.get('option_id')
        option = get_object_or_404(ChatPollOption, id=option_id, poll=poll)

        user_votes = ChatPollVote.objects.filter(poll=poll, user=request.user)

        # 🔥 SINGLE CHOICE
        if not getattr(poll, 'allow_multiple', False):
            if user_votes.filter(option=option).exists():
                if getattr(poll, 'allow_vote_change', True):
                    user_votes.delete()
                else:
                    return JsonResponse({'ok': False, 'error': 'Already voted'}, status=400)
            else:
                user_votes.delete()
                ChatPollVote.objects.create(
                    poll=poll,
                    option=option,
                    user=request.user
                )

        # 🔥 MULTIPLE CHOICE
        else:
            existing = user_votes.filter(option=option).exists()

            if existing:
                if getattr(poll, 'allow_vote_change', True):
                    user_votes.filter(option=option).delete()
                else:
                    return JsonResponse({'ok': False, 'error': 'Cannot change vote'}, status=400)
            else:
                ChatPollVote.objects.create(
                    poll=poll,
                    option=option,
                    user=request.user
                )

        data = {
            'ok': True,
            'poll_id': str(poll.id),
            'poll': _serialize_poll_for_user(poll, request.user),
        }

        _broadcast_poll_update(poll, actor=request.user)

        return JsonResponse(data)

class ClosePollView(LoginRequiredMixin, View):
    def post(self, request, poll_id):
        poll = get_object_or_404(
            ChatPoll.objects.select_related('message', 'message__room'),
            id=poll_id
        )

        if not (poll.created_by_id == request.user.id or request.user.is_superuser):
            return JsonResponse({'ok': False, 'error': 'Only creator can close poll'}, status=403)

        poll.is_closed = True
        poll.save(update_fields=['is_closed'])

        data = {
            'ok': True,
            'poll_id': str(poll.id),
            'poll': _serialize_poll_for_user(poll, request.user),
        }

        _broadcast_poll_update(poll, actor=request.user)

        return JsonResponse(data)


class PollStateView(LoginRequiredMixin, View):
    def post(self, request, room_id):
        room = get_object_or_404(ChatRoom, id=room_id, members=request.user)

        try:
            body = json.loads(request.body.decode('utf-8'))
        except Exception:
            body = {}

        poll_ids = body.get('poll_ids') or []
        polls = ChatPoll.objects.filter(
            id__in=poll_ids,
            message__room=room
        ).prefetch_related('options', 'votes')

        return JsonResponse({
            'ok': True,
            'polls': [_serialize_poll_for_user(p, request.user) for p in polls]
        })


class ChatRoomFileGrantAccessView(LoginRequiredMixin, View):
    """
    Grant file access rights to a specific room member from messaging.
    Uses the existing files.FileShareAccess permission rows.
    """
    def post(self, request, room_id, file_id):
        room = get_object_or_404(ChatRoom, id=room_id, members=request.user)
        if not _can_manage_room(request.user, room):
            return JsonResponse({'ok': False, 'error': 'Only room managers can grant file rights.'}, status=403)

        user_id = (request.POST.get('user_id') or '').strip()
        permission = (request.POST.get('permission') or 'view').strip()

        if permission not in ('view', 'edit', 'full'):
            return JsonResponse({'ok': False, 'error': 'Invalid permission.'}, status=400)

        target_user = get_object_or_404(User, id=user_id)
        if not room.members.filter(id=target_user.id).exists():
            return JsonResponse({'ok': False, 'error': 'User is not in this room.'}, status=400)

        shared_file = get_object_or_404(SharedFile, id=file_id)

        FileShareAccess.objects.update_or_create(
            file=shared_file,
            user=target_user,
            defaults={
                'permission': permission,
                'shared_by': request.user,
            }
        )

        sys_msg = ChatMessage.objects.create(
            room=room,
            sender=request.user,
            message_type='system',
            content=f'🔐 {_safe_full_name(request.user)} granted {permission} access to "{shared_file.name}" for {_safe_full_name(target_user)}.'
        )
        _broadcast_chat_message(sys_msg, viewer=request.user)

        return JsonResponse({
            'ok': True,
            'file_id': str(shared_file.id),
            'user_id': str(target_user.id),
            'permission': permission,
        })


class ChatToolsPaletteView(LoginRequiredMixin, View):
    """
    Returns @-palette suggestions:
    - room members
    - quick tools
    - project files
    """
    def get(self, request, room_id):
        room = get_object_or_404(ChatRoom, id=room_id, members=request.user)
        q = (request.GET.get('q') or '').strip().lower()

        member_qs = room.members.all()
        if q:
            member_qs = member_qs.filter(
                Q(username__icontains=q) |
                Q(first_name__icontains=q) |
                Q(last_name__icontains=q)
            )

        members = [{
            'type': 'member',
            'id': str(u.id),
            'label': _safe_full_name(u),
            'username': getattr(u, 'username', ''),
        } for u in member_qs[:8]]

        tools = [
            {'type': 'tool', 'key': 'task', 'label': 'Create Task'},
            {'type': 'tool', 'key': 'poll', 'label': 'Create Poll'},
            {'type': 'tool', 'key': 'file_access', 'label': 'Grant Project File Access'},
            {'type': 'tool', 'key': 'tracker_create', 'label': 'Create Tracking Sheet'},
            {'type': 'tool', 'key': 'tracker_row', 'label': 'Add Tracking Row'},
            {'type': 'tool', 'key': 'tracker_access', 'label': 'Grant Tracking Sheet Access'},
        ]
        if q:
            tools = [t for t in tools if q in t['label'].lower() or q in t['key']]

        files_data = []
        if room.project_id:
            profile = getattr(request.user, 'staffprofile', None)
            unit = profile.unit if profile else None
            dept = profile.department if profile else None

            file_qs = SharedFile.objects.filter(project=room.project, is_latest=True).filter(
                Q(uploaded_by=request.user) |
                Q(visibility='office') |
                Q(visibility='unit', unit=unit) |
                Q(visibility='department', department=dept) |
                Q(shared_with=request.user) |
                Q(share_access__user=request.user) |
                Q(project__project_manager=request.user) |
                Q(project__team_members=request.user) |
                Q(project__is_public=True)
            ).distinct().order_by('-created_at')

            if q:
                file_qs = file_qs.filter(name__icontains=q)

            files_data = [{
                'type': 'file',
                'id': str(f.id),
                'label': f.name,
                'icon_class': getattr(f, 'icon_class', 'bi bi-file-earmark'),
            } for f in file_qs[:8]]

        return JsonResponse({
            'ok': True,
            'members': members,
            'tools': tools[:8],
            'files': files_data,
        })


class ChatCommandActionView(LoginRequiredMixin, View):
    """
    Handles actions from @ palette.
    """
    def post(self, request, room_id):
        room = get_object_or_404(ChatRoom, id=room_id, members=request.user)
        action = (request.POST.get('action') or '').strip()

        if action == 'create_task':
            title = (request.POST.get('title') or '').strip()
            assigned_to_id = (request.POST.get('assigned_to_id') or '').strip()
            due_date = request.POST.get('due_date') or None
            priority = (request.POST.get('priority') or 'medium').strip()

            if not title or not assigned_to_id:
                return JsonResponse({'ok': False, 'error': 'Task title and assignee are required.'}, status=400)

            assigned_to = get_object_or_404(User, id=assigned_to_id)
            task = Task.objects.create(
                title=title,
                description=(request.POST.get('description') or '').strip(),
                assigned_to=assigned_to,
                assigned_by=request.user,
                priority=priority,
                due_date=due_date,
                project=room.project if room.project_id else None,
            )

            msg = ChatMessage.objects.create(
                room=room,
                sender=request.user,
                message_type='command',
                content=f'✅ Task created: {task.title}',
                command_payload={
                    'command_type': 'task_created',
                    'task_id': str(task.id),
                    'task_title': task.title,
                    'assigned_to': _safe_full_name(assigned_to),
                    'due_date': str(task.due_date) if task.due_date else '',
                }
            )
            _broadcast_chat_message(msg, viewer=request.user)
            return JsonResponse({'ok': True, 'message': _serialize_chat_message(msg, viewer=request.user)})

        if action == 'create_tracker':
            try:
                project = _room_project_or_403(room)
            except PermissionError as e:
                return JsonResponse({'ok': False, 'error': str(e)}, status=400)

            sheet, created = TrackingSheet.objects.get_or_create(
                project=project,
                defaults={
                    'title': f'{project.name} Tracker',
                    'created_by': request.user,
                }
            )
            msg = ChatMessage.objects.create(
                room=room,
                sender=request.user,
                message_type='command',
                content=f'📊 Tracking sheet {"created" if created else "already exists"}: {sheet.title}',
                command_payload={
                    'command_type': 'tracking_sheet',
                    'sheet_id': str(sheet.id),
                    'sheet_title': sheet.title,
                    'project_id': str(project.id),
                }
            )
            _broadcast_chat_message(msg, viewer=request.user)
            return JsonResponse({'ok': True, 'message': _serialize_chat_message(msg, viewer=request.user)})

        if action == 'add_tracker_row':
            try:
                project = _room_project_or_403(room)
            except PermissionError as e:
                return JsonResponse({'ok': False, 'error': str(e)}, status=400)

            sheet = get_object_or_404(TrackingSheet, project=project)
            data = {}
            for col in sheet.get_columns():
                data[col['key']] = request.POST.get(f'data_{col["key"]}', '')

            row = TrackingRow.objects.create(
                sheet=sheet,
                data_json=json.dumps(data),
                order=sheet.rows.count(),
                created_by=request.user,
            )

            msg = ChatMessage.objects.create(
                room=room,
                sender=request.user,
                message_type='command',
                content='📝 Tracking row added.',
                command_payload={
                    'command_type': 'tracking_row',
                    'sheet_id': str(sheet.id),
                    'row_id': str(row.id),
                }
            )
            _broadcast_chat_message(msg, viewer=request.user)
            return JsonResponse({'ok': True, 'message': _serialize_chat_message(msg, viewer=request.user)})

        if action == 'grant_tracker_access':
            try:
                project = _room_project_or_403(room)
            except PermissionError as e:
                return JsonResponse({'ok': False, 'error': str(e)}, status=400)

            if not _can_manage_room(request.user, room):
                return JsonResponse({'ok': False, 'error': 'Only room managers can grant tracker access.'}, status=403)

            sheet = get_object_or_404(TrackingSheet, project=project)
            target_user = get_object_or_404(User, id=request.POST.get('user_id'))
            permission = (request.POST.get('permission') or 'input').strip()

            if permission not in ('view', 'input', 'edit', 'full'):
                return JsonResponse({'ok': False, 'error': 'Invalid permission.'}, status=400)

            TrackingSheetAccess.objects.update_or_create(
                sheet=sheet,
                user=target_user,
                defaults={
                    'permission': permission,
                    'granted_by': request.user,
                }
            )

            msg = ChatMessage.objects.create(
                room=room,
                sender=request.user,
                message_type='system',
                content=f'📊 {_safe_full_name(request.user)} granted {permission} tracker access to {_safe_full_name(target_user)}.'
            )
            _broadcast_chat_message(msg, viewer=request.user)
            return JsonResponse({'ok': True})

        return JsonResponse({'ok': False, 'error': 'Unknown action.'}, status=400)


class SpellCheckView(LoginRequiredMixin, View):
    def post(self, request, room_id):
        get_object_or_404(ChatRoom, id=room_id, members=request.user)
        text = (request.POST.get('text') or '').strip()
        result = _spellcheck_text(text)
        return JsonResponse({
            'ok': True,
            'corrected': result.get('corrected', text),
            'suggestions': result.get('suggestions', []),
        })

# ---------------------------------------------------------------------------
# Presence
# ---------------------------------------------------------------------------
# Cache (Redis) is the source of truth — no DB migration required.
# DB fields updated as best-effort for persistence across Redis restarts.
# ---------------------------------------------------------------------------

import logging as _logging
_presence_log = _logging.getLogger(__name__)

_PRESENCE_TTL     = 90   # cache key lifetime in seconds
_ONLINE_THRESHOLD = 60   # online  if last heartbeat within this many seconds
_IDLE_THRESHOLD   = 300  # idle    if last heartbeat within this many seconds


def _notify_offline_members(room, sender, message):
    """
    After a message is sent, email any room members who are not currently online.
    Runs in a background thread so it never slows down the HTTP response.
    """
    import threading

    def _do_notify():
        try:
            # Get all room members except the sender
            members = room.members.exclude(id=sender.id).select_related('staffprofile')
            for recipient in members:
                try:
                    _send_offline_message_notification(
                        recipient=recipient,
                        sender=sender,
                        room=room,
                        message_content=message.content or '',
                    )
                except Exception:
                    _presence_log.exception('Notification failed for user %s', recipient.pk)
        except Exception:
            _presence_log.exception('_notify_offline_members failed')

    t = threading.Thread(target=_do_notify, daemon=True)
    t.start()


def _presence_cache_key(user_id):
    return f'presence:ls:{user_id}'


def _manual_status_cache_key(user_id):
    return f'presence:manual:{user_id}'


def _set_presence_cache(user_id):
    from django.core.cache import cache
    cache.set(_presence_cache_key(user_id), timezone.now().isoformat(), timeout=_PRESENCE_TTL)


def _set_manual_status_cache(user_id, status):
    """Store manual status with a long TTL (7 days) — persists across sessions."""
    from django.core.cache import cache
    cache.set(_manual_status_cache_key(user_id), status, timeout=7 * 86400)


def _get_manual_status_cache(user_id):
    from django.core.cache import cache
    return cache.get(_manual_status_cache_key(user_id))


def _get_presence_cache(user_id):
    from django.core.cache import cache
    import datetime
    raw = cache.get(_presence_cache_key(user_id))
    if not raw:
        return None
    try:
        dt = datetime.datetime.fromisoformat(raw)
        return timezone.make_aware(dt) if timezone.is_naive(dt) else dt
    except Exception:
        return None


def _user_is_online(user_id):
    """Returns True if the user has a recent heartbeat (within ONLINE_THRESHOLD)."""
    last_seen = _get_presence_cache(user_id)
    if not last_seen:
        return False
    if timezone.is_naive(last_seen):
        last_seen = timezone.make_aware(last_seen)
    delta = (timezone.now() - last_seen).total_seconds()
    return delta < _ONLINE_THRESHOLD


def _user_effective_status(user_id):
    """
    Returns the effective status string for a user.
    Respects manual overrides; falls back to activity-based detection.
    """
    last_seen = _get_presence_cache(user_id)
    if not last_seen:
        return 'offline'
    if timezone.is_naive(last_seen):
        last_seen = timezone.make_aware(last_seen)
    delta = (timezone.now() - last_seen).total_seconds()

    if delta >= _IDLE_THRESHOLD:
        return 'offline'

    manual = _get_manual_status_cache(user_id)
    if manual and manual != 'auto':
        return manual

    return 'online' if delta < _ONLINE_THRESHOLD else 'idle'


def _send_offline_message_notification(recipient, sender, room, message_content):
    """
    Send an email notification to a recipient who is offline/away/busy/dnd
    when they receive a new message.

    Respects DND — DND users do NOT get emails.
    Only sends once per sender per room per 10-minute window to avoid spam.
    """
    from django.core.cache import cache
    from django.core.mail import EmailMessage
    from django.conf import settings

    status = _user_effective_status(recipient.pk)

    # Don't email if they're online or if DND
    if status in ('online', 'available'):
        return
    if status == 'dnd':
        return

    # Rate-limit: max 1 email per sender+room per 10 min per recipient
    rate_key = f'msg_notif:{recipient.pk}:{room.id}:{sender.pk}'
    if cache.get(rate_key):
        return
    cache.set(rate_key, True, timeout=600)

    recipient_email = getattr(recipient, 'email', None)
    if not recipient_email:
        return

    org_name = getattr(settings, 'ORGANISATION_NAME',
                       getattr(settings, 'OFFICE_NAME', 'EasyOffice'))
    from_email = getattr(settings, 'DEFAULT_FROM_EMAIL',
                         f'noreply@{org_name.lower().replace(" ", "")}.org')

    sender_name = _safe_full_name(sender)
    room_name   = room.name or 'a chat'
    preview     = (message_content or '').strip()[:120]
    if len(message_content or '') > 120:
        preview += '…'

    status_label = {
        'offline': 'while you were offline',
        'idle':    'while you were away',
        'away':    'while you were away',
        'busy':    'while you were busy',
    }.get(status, 'while you were away')

    chat_url = getattr(settings, 'SITE_URL', '').rstrip('/') + f'/messages/{room.id}/'

    html = f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"/></head>
<body style="margin:0;padding:0;background:#f1f5f9;font-family:'Segoe UI',Arial,sans-serif;">
<div style="max-width:560px;margin:28px auto;background:#fff;border-radius:14px;overflow:hidden;box-shadow:0 4px 24px rgba(0,0,0,.08)">
  <div style="background:linear-gradient(135deg,#1e3a5f,#6366f1);padding:30px 36px;text-align:center">
    <div style="font-size:2rem;margin-bottom:8px">💬</div>
    <h1 style="margin:0;font-size:18px;color:#fff;font-weight:700">New message {status_label}</h1>
    <p style="margin:4px 0 0;font-size:13px;color:rgba(255,255,255,.75)">{org_name}</p>
  </div>
  <div style="padding:28px 36px">
    <p style="font-size:15px;color:#1e293b;line-height:1.6;margin:0 0 18px">
      <strong>{sender_name}</strong> sent you a message in <strong>{room_name}</strong>:
    </p>
    <div style="background:#f8fafc;border-left:4px solid #6366f1;border-radius:0 8px 8px 0;padding:14px 18px;font-size:14px;color:#334155;line-height:1.6;font-style:italic;margin-bottom:24px">
      {preview if preview else '(file or media attachment)'}
    </div>
    <div style="text-align:center">
      <a href="{chat_url}" style="display:inline-block;background:linear-gradient(135deg,#6366f1,#4f46e5);color:#fff;padding:12px 32px;border-radius:10px;text-decoration:none;font-weight:700;font-size:14px">
        Open Chat
      </a>
    </div>
  </div>
  <div style="background:#f8fafc;padding:16px 36px;border-top:1px solid #e2e8f0;font-size:11px;color:#94a3b8;text-align:center">
    {org_name} · You received this because you were not available. <br>
    To stop these notifications, set your status to Do Not Disturb.
  </div>
</div>
</body></html>"""

    try:
        msg = EmailMessage(
            subject=f'New message from {sender_name} in {room_name} | {org_name}',
            body=html,
            from_email=from_email,
            to=[recipient_email],
        )
        msg.content_subtype = 'html'
        msg.send()
    except Exception:
        _presence_log.exception('Failed to send offline message notification to %s', recipient_email)


def _format_last_seen(dt):
    if not dt:
        return ''
    delta = (timezone.now() - dt).total_seconds()
    if delta < 60:
        return 'just now'
    if delta < 3600:
        return f'{int(delta // 60)} min ago'
    if delta < 86400:
        return f'{int(delta // 3600)}h ago'
    return timezone.localtime(dt).strftime('%b %d')


class PresenceHeartbeatView(LoginRequiredMixin, View):
    """POST /messages/presence/heartbeat/
    Body: manual_status=available|busy|dnd|away|auto
    """

    def post(self, request):
        uid = request.user.pk
        now = timezone.now()

        # Store manual status in cache (separate key, no TTL — persists until changed)
        manual_status = (request.POST.get('manual_status') or '').strip().lower()
        _VALID_MANUAL = {'available', 'busy', 'dnd', 'away', 'auto'}
        if manual_status in _VALID_MANUAL:
            _set_manual_status_cache(uid, manual_status)

        # Always write heartbeat timestamp to cache
        _set_presence_cache(uid)

        # Broadcast presence update to all DM rooms this user is part of
        _broadcast_presence_update(uid)

        # Best-effort DB write
        try:
            request.user.last_seen = now
            request.user.save(update_fields=['last_seen'])
        except Exception as e:
            _presence_log.debug('User.last_seen not saved: %s', e)
            try:
                p = request.user.staffprofile
                p.last_seen = now
                p.save(update_fields=['last_seen'])
            except Exception as e2:
                _presence_log.debug('StaffProfile.last_seen not saved: %s', e2)

        return JsonResponse({'ok': True})


class PresenceView(LoginRequiredMixin, View):
    """GET /messages/presence/<user_id>/"""

    def get(self, request, user_id):
        # 1. Cache (fast, accurate)
        last_seen = _get_presence_cache(user_id)

        # 2. DB fallback (for after Redis restart)
        if last_seen is None:
            try:
                target = User.objects.get(id=user_id)
                last_seen = getattr(target, 'last_seen', None)
                if last_seen is None:
                    try:
                        last_seen = getattr(target.staffprofile, 'last_seen', None)
                    except Exception:
                        pass
            except User.DoesNotExist:
                pass

        if not last_seen:
            return JsonResponse({'status': 'offline', 'last_seen_display': ''})

        if timezone.is_naive(last_seen):
            last_seen = timezone.make_aware(last_seen)

        delta = (timezone.now() - last_seen).total_seconds()

        # Check manual status override
        manual = _get_manual_status_cache(user_id)

        if delta >= _IDLE_THRESHOLD:
            # Heartbeat too old — definitely offline regardless of manual status
            return JsonResponse({'status': 'offline', 'last_seen_display': _format_last_seen(last_seen)})

        if manual and manual != 'auto':
            # User has explicitly set a status and is still active
            return JsonResponse({'status': manual, 'last_seen_display': _format_last_seen(last_seen) if manual != 'available' else ''})

        # Auto mode — derive from activity recency
        if delta < _ONLINE_THRESHOLD:
            return JsonResponse({'status': 'online', 'last_seen_display': ''})
        return JsonResponse({'status': 'idle', 'last_seen_display': _format_last_seen(last_seen)})

import json as _json

_CALL_RING_TTL = 35  # seconds a ring stays "incoming" before auto-clearing


def _call_ring_key(callee_user_id):
    return f'call:ring:{callee_user_id}'


class CallRingView(LoginRequiredMixin, View):
    """
    POST /messages/call/ring/<room_id>/
    Caller presses the "Call" button in a DM. We look up the other member,
    stash a ring record in Redis, and push a WebSocket event on the room
    group so an in-room callee rings instantly.

    NOTE on presence:
      We deliberately do NOT pre-check whether the callee is "online" here.
      Presence in this app is heartbeat-based and the heartbeat only runs
      on the chat_room page — a callee who is logged in and active on a
      different page (dashboard, tasks, etc.) will register as "offline"
      even though they are perfectly reachable. Blocking the call here
      produced false negatives. Instead we always ring; if the callee is
      truly absent the ring record TTL expires and the caller's modal
      eventually times out.
    """

    def post(self, request, room_id):
        room = get_object_or_404(ChatRoom, id=room_id, members=request.user)

        if room.room_type != 'direct':
            return JsonResponse({'ok': False, 'error': 'Only 1-on-1 calls are supported.'}, status=400)

        # Find the OTHER member of the DM
        other = room.members.exclude(id=request.user.id).first()
        if not other:
            return JsonResponse({'ok': False, 'error': 'No one to call.'}, status=400)

        # Only DND is a hard block — the user has explicitly asked not to
        # be disturbed. Everything else (offline, idle, auto, available,
        # busy, away) still rings.
        callee_status = _user_effective_status(other.pk)
        if callee_status == 'dnd':
            return JsonResponse({
                'ok': False,
                'error': f'{_safe_full_name(other)} is on Do Not Disturb.',
            }, status=409)

        from django.core.cache import cache
        ring = {
            'room_id': str(room.id),
            'caller_id': str(request.user.id),
            'caller_name': _safe_full_name(request.user),
            'caller_avatar': (request.user.avatar.url if getattr(request.user, 'avatar', None) else ''),
            'started_at': timezone.now().isoformat(),
        }
        cache.set(_call_ring_key(other.pk), _json.dumps(ring), timeout=_CALL_RING_TTL)

        # Also push over the existing room WS — instant for in-room callees.
        try:
            channel_layer = get_channel_layer()
            if channel_layer:
                async_to_sync(channel_layer.group_send)(
                    _room_group_name(room.id),
                    {
                        'type': 'chat.typing',  # reuse generic relay event
                        'payload': {
                            'type': 'call_ring',
                            **ring,
                        },
                    },
                )
        except Exception:
            pass

        return JsonResponse({
            'ok': True,
            'callee_id': str(other.id),
            'callee_status': callee_status,   # for logging / future UI hints
        })


class CallIncomingView(LoginRequiredMixin, View):
    """
    GET /messages/call/incoming/
    Lightweight poll for the current user. Returns the active ring record
    (if any) or {ok: true, ring: null}.
    """

    def get(self, request):
        from django.core.cache import cache
        raw = cache.get(_call_ring_key(request.user.pk))
        if not raw:
            return JsonResponse({'ok': True, 'ring': None})
        try:
            data = _json.loads(raw) if isinstance(raw, str) else raw
        except Exception:
            data = None
        return JsonResponse({'ok': True, 'ring': data})


class CallClearView(LoginRequiredMixin, View):
    """
    POST /messages/call/clear/<room_id>/
    Clears any ring record for the OTHER member of this DM.
    Called by both sides when the call is accepted / declined / ended.
    """

    def post(self, request, room_id):
        room = get_object_or_404(ChatRoom, id=room_id, members=request.user)
        other = room.members.exclude(id=request.user.id).first()
        if not other:
            return JsonResponse({'ok': True})

        from django.core.cache import cache
        cache.delete(_call_ring_key(other.pk))
        # Also clear a ring pointed at self (e.g. caller cancelling their own ring)
        cache.delete(_call_ring_key(request.user.pk))
        return JsonResponse({'ok': True})