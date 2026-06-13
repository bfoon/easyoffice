"""
apps/poi/scoping.py
───────────────────
Defines EXACTLY what a POI can see/do. Everything the portal returns is filtered
through here, so confinement lives in one place.

Chat:   a POI may only have DM rooms with "admin" and "CEO" users.
Files:  a POI may only see (a) files shared TO them in their DM rooms via
        ChatRoomFile / linked_file messages, and (b) files in their own private
        workspace folder (tool outputs they generate).
Notes:  a POI sees their own notifications (incl. signature requests addressed
        to them) via integrations.notifications_for().
"""

import logging

from django.db.models import Q

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Who counts as an admin / CEO the POI is allowed to message
# ─────────────────────────────────────────────────────────────────────────────
# ‼️ RECONCILE: define how your system marks admins and the CEO. Defaults cover
# superuser/staff + an optional `role`/`title` text match + group membership.
CEO_ROLE_VALUES = {"ceo", "chief executive officer"}
ADMIN_ROLE_VALUES = {"admin", "administrator"}


def allowed_contacts_qs():
    """Queryset of User rows a POI is permitted to chat with (admins + CEO)."""
    from apps.poi.integrations import get_user_model

    User = get_user_model()
    q = Q(is_superuser=True) | Q(is_staff=True)

    # role/title text match (only if the field exists on your User)
    if _user_has_field(User, "role"):
        q |= Q(role__iregex=_iregex(ADMIN_ROLE_VALUES | CEO_ROLE_VALUES))
    if _user_has_field(User, "title"):
        q |= Q(title__iregex=_iregex(CEO_ROLE_VALUES))

    qs = User.objects.filter(q, is_active=True).distinct()

    # Exclude other POIs defensively (a POI must never see/contact another POI).
    qs = qs.exclude(poi_profile__isnull=False)
    return qs


def is_allowed_contact(user):
    return allowed_contacts_qs().filter(pk=user.pk).exists()


# ─────────────────────────────────────────────────────────────────────────────
# Chat rooms
# ─────────────────────────────────────────────────────────────────────────────

def poi_rooms_qs(poi_user):
    """DM rooms the POI is a member of where the OTHER member is an allowed
    contact. Group/unit/project rooms are never shown to a POI."""
    from apps.messaging.models import ChatRoom

    allowed_ids = set(allowed_contacts_qs().values_list("id", flat=True))
    rooms = (
        ChatRoom.objects.filter(members=poi_user, room_type="direct", is_archived=False)
        .prefetch_related("members")
        .order_by("-updated_at")
    )
    keep = []
    for r in rooms:
        others = [m for m in r.members.all() if m.id != poi_user.id]
        if len(others) == 1 and others[0].id in allowed_ids:
            keep.append(r)
    return keep


def get_or_create_dm(poi_user, contact_user):
    """Resolve (or create) the DM room between a POI and an allowed contact.
    Returns None if contact_user isn't an allowed contact."""
    from apps.messaging.models import ChatRoom, ChatRoomMember

    if not is_allowed_contact(contact_user):
        return None

    existing = (
        ChatRoom.objects.filter(room_type="direct", members=poi_user)
        .filter(members=contact_user)
    )
    for r in existing:
        if r.members.count() == 2:
            return r

    room = ChatRoom.objects.create(room_type="direct", created_by=poi_user)
    ChatRoomMember.objects.create(room=room, user=poi_user, role="member")
    ChatRoomMember.objects.create(room=room, user=contact_user, role="member")
    return room


def can_access_room(poi_user, room):
    """True only for DM rooms between this POI and an allowed contact."""
    if room.room_type != "direct":
        return False
    if not room.members.filter(id=poi_user.id).exists():
        return False
    others = [m for m in room.members.all() if m.id != poi_user.id]
    return len(others) == 1 and is_allowed_contact(others[0])


# ─────────────────────────────────────────────────────────────────────────────
# Files
# ─────────────────────────────────────────────────────────────────────────────

def poi_visible_files_qs(poi_user):
    """SharedFiles a POI may see: those shared into their permitted DM rooms,
    plus anything in their own workspace folder."""
    from apps.files.models import SharedFile
    from apps.messaging.models import ChatMessage, ChatRoomFile

    rooms = poi_rooms_qs(poi_user)
    room_ids = [r.id for r in rooms]

    # Files attached/linked in messages of permitted rooms
    linked_ids = set(
        ChatMessage.objects.filter(
            room_id__in=room_ids, linked_file__isnull=False, is_deleted=False
        ).values_list("linked_file_id", flat=True)
    )
    # Files explicitly shared into the room (ChatRoomFile)
    shared_ids = set(
        ChatRoomFile.objects.filter(room_id__in=room_ids).values_list("file_id", flat=True)
    )

    file_ids = linked_ids | shared_ids

    q = Q(id__in=file_ids)

    profile = getattr(poi_user, "poi_profile", None)
    if profile and profile.workspace_folder_id:
        q |= Q(folder_id=profile.workspace_folder_id, uploaded_by=poi_user)

    return SharedFile.objects.filter(q, is_latest=True).select_related(
        "uploaded_by", "folder"
    ).distinct()


def can_access_file(poi_user, shared_file):
    return poi_visible_files_qs(poi_user).filter(pk=shared_file.pk).exists()


# ─────────────────────────────────────────────────────────────────────────────
# helpers
# ─────────────────────────────────────────────────────────────────────────────

def _user_has_field(Model, name):
    try:
        Model._meta.get_field(name)
        return True
    except Exception:
        return False


def _iregex(values):
    # builds a simple case-insensitive alternation: (ceo|administrator|...)
    return r"(" + "|".join(sorted(map(_re_escape, values))) + r")"


def _re_escape(s):
    import re
    return re.escape(s)
