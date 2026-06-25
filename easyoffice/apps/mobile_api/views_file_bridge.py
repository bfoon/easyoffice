"""
apps/mobile_api/views_file_bridge.py
────────────────────────────────────
Two mobile endpoints that move files between the Files app and chat WITHOUT
the phone ever downloading or re-uploading the bytes. Both are pure server-side
operations on SharedFile / ChatMessage, mirroring the web views:

  • attach_from_files  ← web ChatAttachSharedFileView
        Create a ChatMessage that REFERENCES a SharedFile (linked_file).
        No copy: the message points at the existing file.

  • save_to_files      ← web SaveChatMessageToFilesView
        Copy a chat attachment's bytes (read on the SERVER) into a new
        SharedFile owned by the user. The phone sends only the message id.

Routes (add to apps/mobile_api/urls.py):
    path('rooms/<uuid:room_id>/attach-file/',
         views_file_bridge.MobileAttachFileView.as_view(),
         name='mobile_attach_file'),
    path('rooms/<uuid:room_id>/messages/<uuid:message_id>/save-to-files/',
         views_file_bridge.MobileSaveToFilesView.as_view(),
         name='mobile_save_to_files'),
"""

import logging
import os

from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.utils import timezone

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated

log = logging.getLogger(__name__)


def _user_units(user):
    profile = getattr(user, 'staffprofile', None)
    unit = getattr(profile, 'unit', None) if profile else None
    dept = getattr(profile, 'department', None) if profile else None
    return unit, dept


# ─────────────────────────────────────────────────────────────────────────────
# Files  →  Messages   (reference a SharedFile in a chat message; no copy)
# ─────────────────────────────────────────────────────────────────────────────

class MobileAttachFileView(APIView):
    """
    POST /api/mobile/v1/rooms/<room_id>/attach-file/
    Body: { "file_id": "<SharedFile id>", "caption": "", "reply_to": "<msg id>" }

    Mirrors the web ChatAttachSharedFileView: creates a ChatMessage with
    linked_file set, then broadcasts it over the room's channel group so
    everyone (web + mobile) sees it. The file itself is never downloaded.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, room_id):
        from apps.messaging.models import ChatRoom, ChatMessage, ChatRoomMember
        from apps.files.models import SharedFile
        from apps.messaging.views import (
            _serialize_chat_message, _safe_full_name, _notify_offline_members,
        )
        from asgiref.sync import async_to_sync
        from channels.layers import get_channel_layer

        room = get_object_or_404(ChatRoom, id=room_id, members=request.user)
        if room.is_readonly:
            return Response({'error': 'This room is read-only.'}, status=403)

        file_id = (request.data.get('file_id') or '').strip()
        caption = (request.data.get('caption') or '').strip()
        reply_to_id = (request.data.get('reply_to') or '').strip() or None
        if not file_id:
            return Response({'error': 'file_id is required.'}, status=400)

        # Same visibility gate as the web attach view.
        unit, dept = _user_units(request.user)
        shared_file = get_object_or_404(
            SharedFile.objects.filter(
                Q(uploaded_by=request.user)
                | Q(visibility='office')
                | Q(visibility='unit', unit=unit)
                | Q(visibility='department', department=dept)
                | Q(shared_with=request.user)
                | Q(share_access__user=request.user)
            ).distinct(),
            pk=file_id,
        )

        reply_obj = None
        if reply_to_id:
            reply_obj = ChatMessage.objects.filter(
                id=reply_to_id, room=room
            ).select_related('sender').first()

        msg_type = 'image' if shared_file.is_image else 'file'
        msg = ChatMessage.objects.create(
            room=room, sender=request.user, content=caption,
            message_type=msg_type, reply_to=reply_obj,
            linked_file=shared_file, file_name=shared_file.name,
            file_size=shared_file.file_size,
        )

        room.updated_at = timezone.now()
        room.save(update_fields=['updated_at'])
        ChatRoomMember.objects.filter(room=room, user=request.user).update(
            last_read=timezone.now()
        )

        payload = _serialize_chat_message(msg, viewer=request.user)

        # Broadcast to the room group (same event the web/socket use).
        try:
            async_to_sync(get_channel_layer().group_send)(
                f'chat_{room.id}',
                {'type': 'chat.message', 'payload': payload},
            )
        except Exception:
            pass

        # Email offline members + push to mobile devices (same single path).
        try:
            _notify_offline_members(room, request.user, msg)
        except Exception:
            pass

        return Response({'ok': True, 'message': payload}, status=201)


# ─────────────────────────────────────────────────────────────────────────────
# Messages  →  Files   (copy a chat attachment into Files, server-side)
# ─────────────────────────────────────────────────────────────────────────────

class MobileSaveToFilesView(APIView):
    """
    POST /api/mobile/v1/rooms/<room_id>/messages/<message_id>/save-to-files/
    Body: { "folder": "<FileFolder id>"  (optional) }

    Mirrors the web SaveChatMessageToFilesView: reads the message's file bytes
    ON THE SERVER and creates a new private SharedFile owned by the user. The
    phone sends only ids — it never downloads or re-uploads the file.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, room_id, message_id):
        from apps.messaging.models import ChatRoom, ChatMessage
        from apps.files.models import SharedFile, FileFolder
        from apps.messaging.views import _copy_chat_file_to_contentfile

        room = get_object_or_404(ChatRoom, id=room_id, members=request.user)
        msg = get_object_or_404(
            ChatMessage.objects.select_related('sender', 'linked_file'),
            id=message_id, room=room, is_deleted=False,
        )

        if msg.message_type not in ('file', 'image'):
            return Response(
                {'error': 'Only file or image messages can be saved.'}, status=400
            )

        folder = None
        folder_id = (request.data.get('folder') or '').strip()
        if folder_id:
            folder = FileFolder.objects.filter(
                id=folder_id, owner=request.user
            ).first()
            if folder is None:
                return Response({'error': 'Folder not found.'}, status=404)

        # Reads bytes on the SERVER (linked file or attached file).
        file_name, content_file, file_size, mime_type = \
            _copy_chat_file_to_contentfile(msg)
        if not content_file:
            return Response({'error': 'Could not read file data.'}, status=400)

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

        return Response({
            'ok': True,
            'file': {'id': str(saved.id), 'name': saved.name},
        }, status=201)
