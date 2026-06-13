"""
apps/poi/portal_views.py
────────────────────────
The POI portal — a message-style single page plus the JSON endpoints it polls.
Everything is scoped through apps.poi.scoping so a POI can only ever touch their
own DM rooms (admin/CEO), their visible files, and their own notifications.

Routes (see urls.py):
  POIPortalView              GET   /poi/                      → shell template
  POIContactsView            GET   /poi/api/contacts/         → admins+CEO list
  POIRoomsView               GET   /poi/api/rooms/            → POI's DM rooms
  POIRoomMessagesView        GET   /poi/api/rooms/<id>/messages/
  POIRoomSendView            POST  /poi/api/rooms/<id>/send/
  POIStartChatView           POST  /poi/api/chat/start/       → open DM w/ contact
  POIFilesView               GET   /poi/api/files/            → visible files
  POIFilePreviewView         GET   /poi/api/files/<id>/preview/
  POINotificationsView       GET   /poi/api/notifications/
"""

import logging

from django.http import JsonResponse, Http404
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views.generic import View

from apps.poi.guards import POIRequiredMixin
from apps.poi import scoping

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Shell
# ─────────────────────────────────────────────────────────────────────────────

class POIPortalView(POIRequiredMixin, View):
    template_name = "poi/portal.html"

    def get(self, request):
        profile = request.poi_profile
        return render(request, self.template_name, {
            "poi": profile,
            "me": request.user,
            "trusted_until": profile.trusted_until,
        })


# ─────────────────────────────────────────────────────────────────────────────
# Contacts & rooms
# ─────────────────────────────────────────────────────────────────────────────

def _avatar_url(user):
    try:
        if getattr(user, "avatar", None) and user.avatar:
            return user.avatar.url
    except Exception:
        pass
    try:
        sp = getattr(user, "staffprofile", None)
        if sp and getattr(sp, "profile_picture", None) and sp.profile_picture:
            return sp.profile_picture.url
    except Exception:
        pass
    return ""


def _full_name(user):
    try:
        return (user.get_full_name() or "").strip() or getattr(user, "email", "") or "User"
    except Exception:
        return str(user)


class POIContactsView(POIRequiredMixin, View):
    def get(self, request):
        contacts = scoping.allowed_contacts_qs()
        data = [{
            "id": str(u.id),
            "name": _full_name(u),
            "role": ("CEO" if (getattr(u, "is_superuser", False)) else "Admin"),
            "avatar_url": _avatar_url(u),
        } for u in contacts]
        return JsonResponse({"ok": True, "contacts": data})


class POIRoomsView(POIRequiredMixin, View):
    def get(self, request):
        rooms = scoping.poi_rooms_qs(request.user)
        out = []
        for r in rooms:
            others = [m for m in r.members.all() if m.id != request.user.id]
            other = others[0] if others else None
            last = r.messages.filter(is_deleted=False).order_by("-created_at").first()
            preview = ""
            if last:
                preview = ("📎 File" if last.message_type in ("file", "image")
                           else (last.content or "")[:60])
            out.append({
                "room_id": str(r.id),
                "name": _full_name(other) if other else (r.name or "Chat"),
                "avatar_url": _avatar_url(other) if other else "",
                "last_preview": preview,
                "updated_at": timezone.localtime(r.updated_at).isoformat(),
            })
        return JsonResponse({"ok": True, "rooms": out})


class POIStartChatView(POIRequiredMixin, View):
    def post(self, request):
        from apps.poi.integrations import get_user_model
        User = get_user_model()
        contact_id = (request.POST.get("contact_id") or "").strip()
        contact = User.objects.filter(id=contact_id).first()
        if not contact:
            return JsonResponse({"ok": False, "error": "Unknown contact."}, status=404)
        room = scoping.get_or_create_dm(request.user, contact)
        if room is None:
            return JsonResponse({"ok": False, "error": "Not an allowed contact."}, status=403)
        return JsonResponse({"ok": True, "room_id": str(room.id)})


# ─────────────────────────────────────────────────────────────────────────────
# Messages
# ─────────────────────────────────────────────────────────────────────────────

class POIRoomMessagesView(POIRequiredMixin, View):
    def get(self, request, room_id):
        from apps.messaging.models import ChatRoom, ChatRoomMember
        from apps.messaging.views import _serialize_chat_message

        room = get_object_or_404(ChatRoom, id=room_id)
        if not scoping.can_access_room(request.user, room):
            raise Http404()

        after_id = (request.GET.get("after_id") or "").strip()
        qs = room.messages.filter(is_deleted=False).select_related(
            "sender", "reply_to", "reply_to__sender", "linked_file"
        )
        if after_id:
            anchor = room.messages.filter(id=after_id).first()
            if anchor:
                qs = qs.filter(created_at__gt=anchor.created_at)
        qs = qs.order_by("created_at")[:200]

        ChatRoomMember.objects.filter(room=room, user=request.user).update(
            last_read=timezone.now()
        )
        data = [_serialize_chat_message(m, viewer=request.user) for m in qs]
        return JsonResponse({"ok": True, "messages": data})


class POIRoomSendView(POIRequiredMixin, View):
    def post(self, request, room_id):
        from apps.messaging.models import ChatRoom, ChatRoomMember, ChatMessage
        from apps.messaging.views import _serialize_chat_message, _save_mentions

        room = get_object_or_404(ChatRoom, id=room_id)
        if not scoping.can_access_room(request.user, room):
            raise Http404()
        if room.is_readonly:
            return JsonResponse({"ok": False, "error": "Room is read-only."}, status=403)

        content = (request.POST.get("content") or "").strip()
        if len(content) > 5000:
            content = content[:5000]

        # Optional reply target — must be a message in THIS room.
        reply_obj = None
        reply_to_id = (request.POST.get("reply_to") or "").strip()
        if reply_to_id:
            reply_obj = ChatMessage.objects.filter(
                id=reply_to_id, room=room, is_deleted=False
            ).first()

        # Optional uploaded file (POI can share a file straight from the composer).
        upload = request.FILES.get("file")

        if not content and not upload:
            return JsonResponse({"ok": False, "error": "Empty message."}, status=400)

        if upload:
            ctype = (getattr(upload, "content_type", "") or "").lower()
            mtype = "image" if ctype.startswith("image/") else "file"
            msg = ChatMessage.objects.create(
                room=room, sender=request.user, content=content,
                message_type=mtype, reply_to=reply_obj,
                file=upload, file_name=getattr(upload, "name", "") or "",
                file_size=getattr(upload, "size", 0) or 0,
            )
        else:
            msg = ChatMessage.objects.create(
                room=room, sender=request.user, content=content,
                message_type="text", reply_to=reply_obj,
            )
        try:
            _save_mentions(msg)
        except Exception:
            pass
        room.updated_at = timezone.now()
        room.save(update_fields=["updated_at"])
        ChatRoomMember.objects.filter(room=room, user=request.user).update(
            last_read=timezone.now()
        )

        # Broadcast on the same channel group so the admin/CEO sees it live.
        try:
            from asgiref.sync import async_to_sync
            from channels.layers import get_channel_layer
            cl = get_channel_layer()
            if cl is not None:
                async_to_sync(cl.group_send)(
                    f"chat_{room.id}",
                    {"type": "chat.message",
                     "payload": _serialize_chat_message(msg, viewer=request.user)},
                )
        except Exception:
            log.exception("POI send: broadcast failed")

        try:
            from apps.messaging.views import _notify_offline_members
            _notify_offline_members(room, request.user, msg)
        except Exception:
            pass

        return JsonResponse({
            "ok": True,
            "message": _serialize_chat_message(msg, viewer=request.user),
        })


class POIRoomReactView(POIRequiredMixin, View):
    """Toggle an emoji reaction on a message. Mirrors the staff
    ToggleReactionView contract: body `emoji=...` → {ok, reactions}.
    reactions are returned as {emoji: count} for rendering."""

    def post(self, request, room_id, message_id):
        from apps.messaging.models import ChatRoom, ChatMessage

        room = get_object_or_404(ChatRoom, id=room_id)
        if not scoping.can_access_room(request.user, room):
            raise Http404()

        msg = get_object_or_404(ChatMessage, id=message_id, room=room, is_deleted=False)
        emoji = (request.POST.get("emoji") or "").strip()
        if not emoji:
            return JsonResponse({"ok": False, "error": "No emoji."}, status=400)

        reactions = dict(msg.reactions or {})
        uid = str(request.user.id)
        users = list(reactions.get(emoji, []))
        if uid in users:
            users.remove(uid)          # toggle off
        else:
            users.append(uid)          # toggle on
        if users:
            reactions[emoji] = users
        else:
            reactions.pop(emoji, None)  # drop empties
        msg.reactions = reactions
        msg.save(update_fields=["reactions"])

        # Broadcast so the other side updates live (same group + shape staff uses).
        counts = {em: len(us) for em, us in reactions.items()}
        try:
            from asgiref.sync import async_to_sync
            from channels.layers import get_channel_layer
            cl = get_channel_layer()
            if cl is not None:
                async_to_sync(cl.group_send)(
                    f"chat_{room.id}",
                    {"type": "chat.reaction",
                     "payload": {"type": "reaction", "message_id": str(msg.id),
                                 "reactions": counts}},
                )
        except Exception:
            log.exception("POI react: broadcast failed")

        return JsonResponse({"ok": True, "reactions": counts})


class POIRoomEditView(POIRequiredMixin, View):
    """Edit one of the POI's own messages. Mirrors staff EditMessageView:
    FormData `content` → {ok, message}. A POI may only edit messages they sent."""

    def post(self, request, room_id, message_id):
        from apps.messaging.models import ChatRoom, ChatMessage
        from apps.messaging.views import _serialize_chat_message

        room = get_object_or_404(ChatRoom, id=room_id)
        if not scoping.can_access_room(request.user, room):
            raise Http404()

        msg = get_object_or_404(
            ChatMessage, id=message_id, room=room, is_deleted=False
        )
        if msg.sender_id != request.user.id:
            return JsonResponse({"ok": False, "error": "You can only edit your own messages."}, status=403)

        new_content = (request.POST.get("content") or "").strip()
        if not new_content:
            return JsonResponse({"ok": False, "error": "Message cannot be empty."}, status=400)
        if len(new_content) > 5000:
            new_content = new_content[:5000]

        msg.content = new_content
        msg.is_edited = True
        msg.edited_at = timezone.now()
        msg.save(update_fields=["content", "is_edited", "edited_at"])

        try:
            from asgiref.sync import async_to_sync
            from channels.layers import get_channel_layer
            cl = get_channel_layer()
            if cl is not None:
                async_to_sync(cl.group_send)(
                    f"chat_{room.id}",
                    {"type": "chat.edit",
                     "payload": {"type": "edit", "message_id": str(msg.id),
                                 "content": new_content}},
                )
        except Exception:
            log.exception("POI edit: broadcast failed")

        return JsonResponse({
            "ok": True,
            "message": _serialize_chat_message(msg, viewer=request.user),
        })


class POIRoomAttachFileView(POIRequiredMixin, View):
    """Attach an EXISTING file the POI can already see (from their visible set)
    into the room. Mirrors staff ChatAttachSharedFileView: body
    `file_id, caption, reply_to` → {ok, payload}."""

    def post(self, request, room_id):
        from apps.messaging.models import ChatRoom, ChatRoomMember, ChatMessage
        from apps.files.models import SharedFile
        from apps.messaging.views import _serialize_chat_message

        room = get_object_or_404(ChatRoom, id=room_id)
        if not scoping.can_access_room(request.user, room):
            raise Http404()
        if room.is_readonly:
            return JsonResponse({"ok": False, "error": "Room is read-only."}, status=403)

        file_id = (request.POST.get("file_id") or "").strip()
        sf = get_object_or_404(SharedFile, id=file_id)
        if not scoping.can_access_file(request.user, sf):
            raise Http404()

        caption = (request.POST.get("caption") or "").strip()
        reply_to_id = (request.POST.get("reply_to") or "").strip()
        reply_obj = None
        if reply_to_id:
            reply_obj = ChatMessage.objects.filter(
                id=reply_to_id, room=room, is_deleted=False
            ).first()

        msg = ChatMessage.objects.create(
            room=room, sender=request.user, content=caption,
            message_type="file", linked_file=sf, reply_to=reply_obj,
        )
        room.updated_at = timezone.now()
        room.save(update_fields=["updated_at"])
        ChatRoomMember.objects.filter(room=room, user=request.user).update(
            last_read=timezone.now()
        )

        payload = _serialize_chat_message(msg, viewer=request.user)
        try:
            from asgiref.sync import async_to_sync
            from channels.layers import get_channel_layer
            cl = get_channel_layer()
            if cl is not None:
                async_to_sync(cl.group_send)(
                    f"chat_{room.id}", {"type": "chat.message", "payload": payload},
                )
        except Exception:
            log.exception("POI attach: broadcast failed")

        return JsonResponse({"ok": True, "payload": payload})


# ─────────────────────────────────────────────────────────────────────────────
# Files
# ─────────────────────────────────────────────────────────────────────────────

class POIFilesView(POIRequiredMixin, View):
    def get(self, request):
        files = scoping.poi_visible_files_qs(request.user)
        out = [{
            "id": str(f.id),
            "name": f.name,
            "size": getattr(f, "file_size", 0),
            "type": getattr(f, "file_type", "") or "",
            "uploaded_by": _full_name(f.uploaded_by) if f.uploaded_by else "",
            "is_pdf": (f.name or "").lower().endswith(".pdf"),
        } for f in files]
        return JsonResponse({"ok": True, "files": out})


class POIFilePreviewView(POIRequiredMixin, View):
    def get(self, request, file_id):
        from apps.files.models import SharedFile
        f = get_object_or_404(SharedFile, id=file_id)
        if not scoping.can_access_file(request.user, f):
            raise Http404()
        try:
            url = f.file.url
        except Exception:
            url = ""
        return JsonResponse({
            "ok": True,
            "name": f.name,
            "url": url,
            "type": getattr(f, "file_type", "") or "",
        })


# ─────────────────────────────────────────────────────────────────────────────
# Notifications
# ─────────────────────────────────────────────────────────────────────────────

class POINotificationsView(POIRequiredMixin, View):
    def get(self, request):
        from apps.poi.integrations import notifications_for
        notifs = notifications_for(request.user, limit=50)
        out = []
        for n in notifs:
            out.append({
                "id": str(getattr(n, "id", "")),
                "text": (getattr(n, "message", "") or getattr(n, "text", "")
                         or getattr(n, "verb", "") or str(n)),
                "is_read": bool(getattr(n, "is_read", False)),
                "created_at": (
                    timezone.localtime(getattr(n, "created_at", timezone.now())).isoformat()
                ),
                "url": getattr(n, "url", "") or getattr(n, "link", "") or "",
            })
        return JsonResponse({"ok": True, "notifications": out})