from django.contrib.auth.mixins import LoginRequiredMixin
from django.views.generic import TemplateView, View
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages as django_messages
from django.db.models import Q
from django.http import HttpResponseForbidden
from django.utils import timezone

from apps.messaging.models import ChatRoom, ChatRoomMember, ChatMessage
from apps.core.models import User


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _sidebar_rooms(user):
    """Return rooms split by type for the sidebar, with unread counts."""
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
            last_read = membership.last_read
        except ChatRoomMember.DoesNotExist:
            last_read = None

        unread = room.messages.filter(is_deleted=False).exclude(sender=user)
        if last_read:
            unread = unread.filter(created_at__gt=last_read)
        unread_count = unread.count()

        last_msg = room.messages.filter(is_deleted=False).order_by('-created_at').first()

        rooms_with_meta.append({
            'room': room,
            'unread': unread_count,
            'last_message': last_msg,
        })
    return rooms_with_meta


def _can_manage_room(user, room):
    """
    Room creator and superuser can manage membership and delete the room.
    """
    if user.is_superuser:
        return True
    return room.created_by_id == user.id


def _can_post_in_room(user, room):
    if room.is_readonly:
        return False
    return room.members.filter(id=user.id).exists()


# ---------------------------------------------------------------------------
# Chat List
# ---------------------------------------------------------------------------

class ChatListView(LoginRequiredMixin, TemplateView):
    template_name = 'messaging/chat_list.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['rooms_meta'] = _sidebar_rooms(self.request.user)
        ctx['all_staff'] = (
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
            .select_related('sender', 'reply_to', 'reply_to__sender')
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
            'room': room,
            'chat_messages': chat_messages,
            'members': members,
            'rooms_meta': _sidebar_rooms(request.user),
            'all_staff': (
                User.objects.filter(status='active', is_active=True)
                .exclude(id__in=existing_member_ids)
                .order_by('first_name')
            ),
            'room_types': ChatRoom.RoomType.choices,
            'can_manage_room': _can_manage_room(request.user, room),
        }
        return render(request, self.template_name, ctx)

    def post(self, request, room_id):
        room = get_object_or_404(ChatRoom, id=room_id, members=request.user)

        if not _can_post_in_room(request.user, room):
            django_messages.warning(request, 'This room is read-only.')
            return redirect('chat_room', room_id=room_id)

        content = request.POST.get('content', '').strip()
        reply_to_id = request.POST.get('reply_to')
        reply_to = None

        if reply_to_id:
            try:
                reply_to = ChatMessage.objects.get(id=reply_to_id, room=room)
            except ChatMessage.DoesNotExist:
                pass

        if 'file' in request.FILES:
            f = request.FILES['file']
            msg_type = 'image' if f.content_type.startswith('image/') else 'file'
            ChatMessage.objects.create(
                room=room,
                sender=request.user,
                message_type=msg_type,
                file=f,
                file_name=f.name,
                file_size=f.size,
                content=content,
                reply_to=reply_to,
            )
            room.save()

        elif content:
            ChatMessage.objects.create(
                room=room,
                sender=request.user,
                content=content,
                message_type='text',
                reply_to=reply_to,
            )
            room.save()

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
            name=f'{request.user.full_name} & {target.full_name}',
            room_type='direct',
            created_by=request.user,
        )
        ChatRoomMember.objects.create(room=room, user=request.user, role='admin')
        ChatRoomMember.objects.create(room=room, user=target, role='admin')
        return redirect('chat_room', room_id=room.id)


# ---------------------------------------------------------------------------
# Create Chat Room
# ---------------------------------------------------------------------------

class CreateChatRoomView(LoginRequiredMixin, View):
    def get(self, request):
        return redirect('chat_list')

    def post(self, request):
        name = request.POST.get('name', '').strip()
        room_type = request.POST.get('room_type', 'group')

        if not name:
            django_messages.error(request, 'Room name is required.')
            return redirect('chat_list')

        room = ChatRoom.objects.create(
            name=name,
            room_type=room_type,
            created_by=request.user,
            description=request.POST.get('description', ''),
        )

        ChatRoomMember.objects.create(room=room, user=request.user, role='admin')

        for uid in request.POST.getlist('members'):
            try:
                u = User.objects.get(id=uid, is_active=True)
                ChatRoomMember.objects.get_or_create(
                    room=room,
                    user=u,
                    defaults={'role': 'member'}
                )
            except User.DoesNotExist:
                pass

        django_messages.success(request, f'"{room.name}" created.')
        return redirect('chat_room', room_id=room.id)


# ---------------------------------------------------------------------------
# Add Room Member
# ---------------------------------------------------------------------------

class AddRoomMemberView(LoginRequiredMixin, View):
    def post(self, request, room_id):
        room = get_object_or_404(ChatRoom, id=room_id, members=request.user)

        if not _can_manage_room(request.user, room):
            return HttpResponseForbidden('Only the room creator can add members.')

        if room.room_type == ChatRoom.RoomType.DIRECT:
            django_messages.error(request, 'Direct messages cannot have extra members.')
            return redirect('chat_room', room_id=room.id)

        member_ids = request.POST.getlist('members')
        added_count = 0

        for uid in member_ids:
            try:
                user = User.objects.get(id=uid, is_active=True, status='active')
            except User.DoesNotExist:
                continue

            _, created = ChatRoomMember.objects.get_or_create(
                room=room,
                user=user,
                defaults={'role': 'member'}
            )
            if created:
                added_count += 1

        if added_count:
            django_messages.success(request, f'{added_count} member(s) added to "{room.name}".')
        else:
            django_messages.info(request, 'No new members were added.')

        return redirect('chat_room', room_id=room.id)


# ---------------------------------------------------------------------------
# Remove Room Member
# ---------------------------------------------------------------------------

class RemoveRoomMemberView(LoginRequiredMixin, View):
    def post(self, request, room_id, user_id):
        room = get_object_or_404(ChatRoom, id=room_id, members=request.user)

        if not _can_manage_room(request.user, room):
            return HttpResponseForbidden('Only the room creator can remove members.')

        if room.room_type == ChatRoom.RoomType.DIRECT:
            django_messages.error(request, 'Direct message participants cannot be removed.')
            return redirect('chat_room', room_id=room.id)

        member = get_object_or_404(User, id=user_id)

        if member.id == room.created_by_id:
            django_messages.error(request, 'The room creator cannot be removed from the room.')
            return redirect('chat_room', room_id=room.id)

        deleted, _ = ChatRoomMember.objects.filter(room=room, user=member).delete()

        if deleted:
            django_messages.success(request, f'{member.full_name} removed from "{room.name}".')
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