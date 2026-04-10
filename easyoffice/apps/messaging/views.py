from django.contrib.auth.mixins import LoginRequiredMixin
from django.views.generic import TemplateView, View
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages as django_messages
from django.db.models import Q
from django.http import HttpResponseForbidden, JsonResponse
from django.utils import timezone
from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer

from apps.messaging.models import ChatRoom, ChatRoomMember, ChatMessage
from apps.core.models import User


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _sidebar_rooms(user):
    """Return rooms for the sidebar with unread counts and last message."""
    all_rooms = (
        ChatRoom.objects.filter(members=user, is_archived=False)
        .select_related('created_by')
        .prefetch_related('chatroommember_set')
        .order_by('-updated_at')
    )
    rooms_with_meta = []
    for room in all_rooms:
        try:
            membership = room.chatroommember_set.get(user=user)
            last_read  = membership.last_read
        except ChatRoomMember.DoesNotExist:
            last_read = None

        unread = room.messages.filter(is_deleted=False).exclude(sender=user)
        if last_read:
            unread = unread.filter(created_at__gt=last_read)
        unread_count = unread.count()

        last_msg = room.messages.filter(is_deleted=False).order_by('-created_at').first()

        rooms_with_meta.append({
            'room':         room,
            'unread':       unread_count,
            'last_message': last_msg,
        })
    return rooms_with_meta


def _can_manage_room(user, room):
    if user.is_superuser:
        return True
    return room.created_by_id == user.id


def _can_post_in_room(user, room):
    if room.is_readonly:
        return False
    return room.members.filter(id=user.id).exists()


def _safe_full_name(user):
    try:
        return user.full_name or user.get_full_name() or user.username
    except Exception:
        return str(user)


def _safe_initials(user):
    try:
        if getattr(user, 'initials', None):
            return user.initials
    except Exception:
        pass
    name  = _safe_full_name(user).strip()
    if not name:
        return '?'
    parts = name.split()
    return ''.join(p[0].upper() for p in parts[:2]) or '?'


def _room_group_name(room_id):
    return f'chat_{room_id}'


def _get_msg_file_url(msg):
    """
    Return the best available file URL for a message.
    Handles both direct uploads (msg.file) and Files-app references (msg.linked_file).
    """
    if getattr(msg, 'file', None):
        try:
            return msg.file.url
        except Exception:
            pass
    # linked_file (FK added in messaging migration)
    if getattr(msg, 'linked_file_id', None):
        try:
            lf = msg.linked_file
            if lf and lf.file:
                return lf.file.url
        except Exception:
            pass
    return ''


def _get_msg_file_name(msg):
    if msg.file_name:
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
    if msg.file_size:
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
    Convert stored JSON { "👍": ["uid1", "uid2"] } to { "👍": 2 }.
    Handles both list-of-user-ids and legacy int formats.
    """
    out = {}
    for emoji, users in (reactions or {}).items():
        if isinstance(users, list):
            out[emoji] = len(users)
        elif isinstance(users, int):
            out[emoji] = users
    return out


def _toggle_reaction_on_message(message, user, emoji):
    """
    Toggle a reaction. Stored as { "👍": ["user_id_1", ...] }.
    Returns the updated raw reactions dict.
    """
    reactions = message.reactions or {}
    user_id   = str(user.id)

    emoji_users = reactions.get(emoji, [])

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
    return reactions


def _serialize_chat_message(msg):
    """Build the full payload dict for a saved ChatMessage."""
    file_url  = _get_msg_file_url(msg)
    file_name = _get_msg_file_name(msg)
    file_size = _get_msg_file_size(msg)

    # file_size as a human-readable string if it came from a linked SharedFile
    if not msg.file_size and getattr(msg, 'linked_file_id', None):
        try:
            lf = msg.linked_file
            if lf:
                file_size_display = lf.size_display
            else:
                file_size_display = str(file_size)
        except Exception:
            file_size_display = str(file_size)
    else:
        file_size_display = str(file_size) if file_size else ''

    return {
        'type':             'chat_message',
        'message_id':       str(msg.id),
        'room_id':          str(msg.room_id),
        'sender_id':        str(msg.sender_id),
        'sender_name':      _safe_full_name(msg.sender),
        'sender_initials':  _safe_initials(msg.sender),
        'message':          msg.content or '',
        'content':          msg.content or '',
        'message_type':     msg.message_type,
        'timestamp':        timezone.localtime(msg.created_at).strftime('%H:%M'),
        'created_at':       timezone.localtime(msg.created_at).isoformat(),
        'reply_to_id':      str(msg.reply_to_id) if msg.reply_to_id else None,
        'reply_to_sender':  (
            _safe_full_name(msg.reply_to.sender)
            if msg.reply_to_id and msg.reply_to and msg.reply_to.sender
            else ''
        ),
        'reply_to_content': (
            msg.reply_to.content[:60]
            if msg.reply_to_id and msg.reply_to and msg.reply_to.content
            else ''
        ),
        'file_url':   file_url,
        'file_name':  file_name,
        'file_size':  file_size_display,
        'is_edited':  getattr(msg, 'is_edited', False),
        'reactions':  _reaction_summary(getattr(msg, 'reactions', {})),
    }


def _broadcast_chat_message(msg):
    """Push a saved ChatMessage to all WS clients in the room."""
    try:
        channel_layer = get_channel_layer()
        if channel_layer is None:
            return
        payload = _serialize_chat_message(msg)
        async_to_sync(channel_layer.group_send)(
            _room_group_name(msg.room_id),
            {'type': 'chat.message', 'payload': payload},
        )
    except Exception:
        pass


def _broadcast_reaction(room_id, message_id, summary):
    """Push a reaction update to all WS clients in the room."""
    try:
        channel_layer = get_channel_layer()
        if channel_layer is None:
            return
        async_to_sync(channel_layer.group_send)(
            _room_group_name(room_id),
            {
                'type': 'chat.reaction',
                'payload': {
                    'type':       'chat_reaction',
                    'room_id':    str(room_id),
                    'message_id': str(message_id),
                    'reactions':  summary,
                },
            },
        )
    except Exception:
        pass


def _broadcast_typing(room_id, sender):
    try:
        channel_layer = get_channel_layer()
        if channel_layer is None:
            return
        async_to_sync(channel_layer.group_send)(
            _room_group_name(room_id),
            {
                'type': 'chat.typing',
                'payload': {
                    'type':        'typing',
                    'room_id':     str(room_id),
                    'sender_id':   str(sender.id),
                    'sender_name': _safe_full_name(sender),
                },
            }
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Chat List
# ---------------------------------------------------------------------------

class ChatListView(LoginRequiredMixin, TemplateView):
    template_name = 'messaging/chat_list.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['rooms_meta'] = _sidebar_rooms(self.request.user)
        ctx['all_staff']  = (
            User.objects.filter(status='active', is_active=True)
            .exclude(id=self.request.user.id)
            .select_related('staffprofile', 'staffprofile__position', 'staffprofile__department')
            .order_by('first_name')
        )
        ctx['room_types'] = ChatRoom.RoomType.choices
        return ctx


# ---------------------------------------------------------------------------
# Chat Room
# ---------------------------------------------------------------------------

class ChatRoomView(LoginRequiredMixin, View):
    template_name = 'messaging/chat_room.html'

    def get(self, request, room_id):
        room = get_object_or_404(ChatRoom, id=room_id, members=request.user)

        chat_messages = (
            room.messages.filter(is_deleted=False)
            .select_related('sender', 'reply_to', 'reply_to__sender', 'linked_file')
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

        ctx = {
            'room':            room,
            'chat_messages':   chat_messages,
            'members':         members,
            'rooms_meta':      _sidebar_rooms(request.user),
            'all_staff': (
                User.objects.filter(status='active', is_active=True)
                .exclude(id__in=existing_member_ids)
                .order_by('first_name')
            ),
            'room_types':      ChatRoom.RoomType.choices,
            'can_manage_room': _can_manage_room(request.user, room),
        }
        return render(request, self.template_name, ctx)

    def post(self, request, room_id):
        room = get_object_or_404(ChatRoom, id=room_id, members=request.user)

        if not _can_post_in_room(request.user, room):
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
                room         = room,
                sender       = request.user,
                message_type = msg_type,
                file         = f,
                file_name    = f.name,
                file_size    = f.size,
                content      = content,
                reply_to     = reply_to,
            )

        elif content:
            new_message = ChatMessage.objects.create(
                room         = room,
                sender       = request.user,
                content      = content,
                message_type = 'text',
                reply_to     = reply_to,
            )

        if new_message:
            room.updated_at = timezone.now()
            room.save(update_fields=['updated_at'])
            ChatRoomMember.objects.filter(room=room, user=request.user).update(
                last_read=timezone.now()
            )
            _broadcast_chat_message(new_message)

        return redirect('chat_room', room_id=room_id)


# ---------------------------------------------------------------------------
# Direct Message
# ---------------------------------------------------------------------------

class DirectMessageView(LoginRequiredMixin, View):
    def get(self, request, user_id):
        target = get_object_or_404(User, id=user_id)

        existing = (
            ChatRoom.objects.filter(room_type='direct', members=request.user)
            .filter(members=target)
            .first()
        )
        if existing:
            return redirect('chat_room', room_id=existing.id)

        room = ChatRoom.objects.create(
            name       = f'{_safe_full_name(request.user)} & {_safe_full_name(target)}',
            room_type  = 'direct',
            created_by = request.user,
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
        name      = request.POST.get('name', '').strip()
        room_type = request.POST.get('room_type', 'group')

        if not name:
            django_messages.error(request, 'Room name is required.')
            return redirect('chat_list')

        room = ChatRoom.objects.create(
            name        = name,
            room_type   = room_type,
            created_by  = request.user,
            description = request.POST.get('description', ''),
        )
        ChatRoomMember.objects.create(room=room, user=request.user, role='admin')

        for uid in request.POST.getlist('members'):
            try:
                u = User.objects.get(id=uid, is_active=True)
                ChatRoomMember.objects.get_or_create(
                    room=room, user=u, defaults={'role': 'member'}
                )
            except User.DoesNotExist:
                pass

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
    """
    GET /messages/<room_id>/messages/poll/?after_id=<uuid>
    Returns new messages since the given anchor message.
    Includes reactions so the client can update stale reaction counts.
    """

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

        ChatRoomMember.objects.filter(room=room, user=request.user).update(
            last_read=timezone.now()
        )

        return JsonResponse({'ok': True, 'messages': messages_data})


# ---------------------------------------------------------------------------
# Reactions poll  ← NEW — auto-refreshes reaction counts without WS
# ---------------------------------------------------------------------------

class ChatReactionsPollView(LoginRequiredMixin, View):
    """
    GET /messages/<room_id>/reactions-poll/
    Returns a mapping { message_id: { emoji: count } } for all visible
    messages in the room (capped at 200 most recent).

    The front-end polls this every 4 s and calls renderReactions() on each
    entry so emoji counts stay in sync even when the WebSocket is down or
    another tab has reacted.
    """

    def get(self, request, room_id):
        room = get_object_or_404(ChatRoom, id=room_id, members=request.user)

        qs = (
            room.messages
            .filter(is_deleted=False)
            .exclude(reactions={})          # skip messages with no reactions
            .order_by('-created_at')
            .values('id', 'reactions')
            [:200]
        )

        data = {}
        for row in qs:
            summary = _reaction_summary(row['reactions'])
            if summary:                     # only include if there are actual counts
                data[str(row['id'])] = summary

        return JsonResponse({'ok': True, 'reactions': data})


# ---------------------------------------------------------------------------
# Toggle Reaction
# ---------------------------------------------------------------------------

class ToggleReactionView(LoginRequiredMixin, View):
    """
    POST /messages/<room_id>/message/<message_id>/react/
    Toggle a reaction emoji; broadcasts the update via WS.
    """

    def post(self, request, room_id, message_id):
        room    = get_object_or_404(ChatRoom,    id=room_id,    members=request.user)
        message = get_object_or_404(ChatMessage, id=message_id, room=room, is_deleted=False)

        emoji = (request.POST.get('emoji') or '').strip()
        if not emoji:
            return JsonResponse({'ok': False, 'error': 'Emoji is required.'}, status=400)

        reactions = _toggle_reaction_on_message(message, request.user, emoji)
        summary   = _reaction_summary(reactions)

        # Broadcast to all WS clients in the room
        _broadcast_reaction(room.id, message.id, summary)

        return JsonResponse({'ok': True, 'message_id': str(message.id), 'reactions': summary})


# ---------------------------------------------------------------------------
# Delete message (soft delete)
# ---------------------------------------------------------------------------

class DeleteMessageView(LoginRequiredMixin, View):
    """POST /messages/api/delete/<message_id>/"""

    def post(self, request, message_id):
        msg = get_object_or_404(
            ChatMessage,
            id         = message_id,
            sender     = request.user,
            is_deleted = False,
        )
        msg.is_deleted = True
        msg.deleted_at = timezone.now()
        msg.save(update_fields=['is_deleted', 'deleted_at'])
        return JsonResponse({'ok': True})


# ---------------------------------------------------------------------------
# Files-app picker API  ← NEW
# ---------------------------------------------------------------------------

class ChatFilePickerAPIView(LoginRequiredMixin, View):
    """
    GET /messages/<room_id>/files/?q=<search>
    Returns the requesting user's accessible SharedFiles as JSON for the
    in-chat file picker modal.
    """

    def get(self, request, room_id):
        from apps.files.models import SharedFile

        # Confirm the user is a member of this room
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
                continue  # skip files with broken storage
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
# Attach Files-app file to chat  ← NEW
# ---------------------------------------------------------------------------

class ChatAttachSharedFileView(LoginRequiredMixin, View):
    """
    POST /messages/<room_id>/attach-file/
    Attaches an existing SharedFile to a chat message.
    No re-upload; creates a ChatMessage with linked_file set,
    then broadcasts the payload via WebSocket.

    POST params:
        file_id   — UUID of the SharedFile
        caption   — optional text
        reply_to  — optional ChatMessage UUID
    """

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

        # Check the user can access this file (same visibility rules as the Files app)
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

        # Resolve optional reply
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
            room         = room,
            sender       = request.user,
            content      = caption,
            message_type = msg_type,
            reply_to     = reply_obj,
            linked_file  = shared_file,
            file_name    = shared_file.name,
            file_size    = shared_file.file_size,
        )

        room.updated_at = timezone.now()
        room.save(update_fields=['updated_at'])
        ChatRoomMember.objects.filter(room=room, user=request.user).update(last_read=timezone.now())

        # Build payload (reuse serialiser; msg.linked_file is already the obj)
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

        # Broadcast to the room's WS group
        try:
            channel_layer = get_channel_layer()
            if channel_layer:
                async_to_sync(channel_layer.group_send)(
                    _room_group_name(room_id),
                    {'type': 'chat.message', 'payload': payload},
                )
        except Exception:
            pass  # WS unavailable — polling will pick it up

        return JsonResponse({'ok': True, 'payload': payload})