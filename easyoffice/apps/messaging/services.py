"""
apps/messaging/services.py
──────────────────────────
Messages that other apps ask the chat to send.

Right now that is one thing: when a signature request completes, every
participant gets the signed PDF as a real file message in their direct
conversation with the person who sent it for signature — the same kind of
message a colleague would send by attaching a file from the file picker, so
it renders identically on web and in the Flutter client, and lands in the
room's file list.

The attachment is a ``linked_file`` reference to the ``SharedFile``, not a
second copy of the bytes. That matters: the file stays governed by the files
app's ACLs, so revoking someone's access to the document also stops the chat
attachment from opening. It also means one PDF on disk instead of one per
signer.

Called through ``apps.files.signature_delivery``; that module resolves the
handler from ``settings.SIGNATURE_CHAT_DELIVERY`` and falls back to
``deliver_signed_document`` below, so no settings change is needed.
"""
from __future__ import annotations

import logging

from django.utils import timezone

from apps.messaging.models import ChatMessage, ChatRoom, ChatRoomMember

logger = logging.getLogger(__name__)


# ── Room resolution ─────────────────────────────────────────────────────────

def get_or_create_direct_room(user_a, user_b):
    """
    The one-to-one room between two users, created if it does not exist.

    Mirrors DirectMessageView so a room opened from the chat sidebar and a
    room created by a system delivery are the same room — a signer should not
    end up with two parallel conversations with the same person.
    """
    from apps.messaging.views import _safe_full_name

    if not user_a or not user_b or user_a.pk == user_b.pk:
        return None

    existing = (
        ChatRoom.objects
        .filter(room_type=ChatRoom.RoomType.DIRECT, members=user_a)
        .filter(members=user_b)
        .first()
    )
    if existing:
        return existing

    room = ChatRoom.objects.create(
        name=f'{_safe_full_name(user_a)} & {_safe_full_name(user_b)}',
        room_type=ChatRoom.RoomType.DIRECT,
        created_by=user_a,
    )
    ChatRoomMember.objects.create(room=room, user=user_a, role=ChatRoomMember.Role.ADMIN)
    ChatRoomMember.objects.create(room=room, user=user_b, role=ChatRoomMember.Role.ADMIN)
    return room


# ── Generic: post a SharedFile into a room ──────────────────────────────────

def post_shared_file(room, sender, shared_file, caption='', *,
                     notify=True, list_in_room_files=True, note=''):
    """
    Create a file message linking an existing SharedFile, broadcast it, and
    run the offline-member email/push path.

    Deliberately reuses the helpers the interactive attach endpoint uses, so
    a system-sent attachment behaves exactly like a user-sent one.

    ``list_in_room_files`` also records a ChatRoomFile row, which is what puts
    the document in the room's Shared Files tab. Note what that row means in
    this codebase: ChatRoomFile grants every room member access to the file,
    bypassing normal file visibility. That is safe for the signed-copy case —
    a direct room has exactly two members and both already hold an explicit
    FileShareAccess row on the document — but pass False for any room whose
    membership is wider than the set of people entitled to the file.
    """
    from apps.messaging.views import _broadcast_chat_message, _notify_offline_members

    if not room or not shared_file:
        return None

    message = ChatMessage.objects.create(
        room=room,
        sender=sender,
        content=caption or '',
        message_type=(
            ChatMessage.MessageType.IMAGE
            if getattr(shared_file, 'is_image', False)
            else ChatMessage.MessageType.FILE
        ),
        linked_file=shared_file,
        file_name=shared_file.name,
        file_size=shared_file.file_size or 0,
    )

    try:
        room.updated_at = timezone.now()
        room.save(update_fields=['updated_at'])
    except Exception:
        logger.exception('Could not bump room %s', room.pk)

    # The sender has, by definition, already seen what they just sent. Without
    # this their own delivery comes back as an unread badge — the interactive
    # attach endpoint does the same thing for the same reason.
    if sender is not None:
        try:
            ChatRoomMember.objects.filter(room=room, user=sender).update(
                last_read=timezone.now()
            )
        except Exception:
            logger.exception('Could not advance last_read for %s', sender.pk)

    if list_in_room_files:
        try:
            from apps.messaging.models import ChatRoomFile
            ChatRoomFile.objects.get_or_create(
                room=room,
                file=shared_file,
                defaults={'shared_by': sender, 'note': note},
            )
        except Exception:
            logger.exception('Could not list %s in room files', shared_file.pk)

    try:
        _broadcast_chat_message(message, viewer=sender)
    except Exception:
        logger.exception('Could not broadcast message %s', message.pk)

    if notify and sender is not None:
        try:
            _notify_offline_members(room, sender, message)
        except Exception:
            logger.exception('Could not notify offline members for %s', message.pk)

    return message


# ── The signature hook ──────────────────────────────────────────────────────

def deliver_signed_document(*, recipient, sender, signed_file,
                            signature_request, download_url=''):
    """
    Deliver the signed PDF to one participant as a chat message.

    Signature is keyword-only and fixed — ``signature_delivery`` calls any
    configured handler this way, so a replacement implementation can be
    dropped in without touching the files app.

    Returns the created ChatMessage, or None when there is nothing to do
    (no recipient, no sender, or the two are the same person).
    """
    if not recipient or not sender or recipient.pk == sender.pk:
        return None

    room = get_or_create_direct_room(sender, recipient)
    if room is None:
        return None

    signer_count = signature_request.signers.count()
    caption = (
        f'✅ "{signature_request.title}" is fully signed by all '
        f'{signer_count} signer(s). Your copy is attached and has also been '
        f'added to your files.'
    )

    return post_shared_file(
        room, sender, signed_file, caption,
        note=f'Signed copy — {signature_request.title}'[:200],
    )
