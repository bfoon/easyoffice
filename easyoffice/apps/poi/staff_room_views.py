"""
apps/poi/staff_room_views.py
────────────────────────────
Staff-side endpoints that let an admin/CEO add (or remove) named staff to a
POI channel — the single, auditable exception to the POI confinement rule.

Security model
--------------
* Gated by AdminPOIManageMixin (staff/superuser/admin-role) — a POI can never
  reach these endpoints, and can never add anyone to any room.
* Adding a user records the grant on POIRoomExtension for THAT room only. The
  scoping layer (apps.poi.scoping) consults this per-room opt-in; it never
  loosens the rule for any other room.
* A POI is NEVER a valid staff member to add — hard-blocked here regardless of
  anything else. This keeps "a POI is never in a room with another POI" intact.
* The room must already contain a POI (otherwise there's nothing to extend and
  no exception to make).

Wire in urls.py:
    path("manage/rooms/", staff_room_views.POIChannelListView.as_view(), ...)
    path("manage/rooms/<uuid:room_id>/add-staff/", staff_room_views.POIChannelAddStaffView.as_view(), ...)
    path("manage/rooms/<uuid:room_id>/remove-staff/<uuid:user_id>/", staff_room_views.POIChannelRemoveStaffView.as_view(), ...)
"""

import logging

from django.http import JsonResponse, Http404
from django.shortcuts import get_object_or_404
from django.views.generic import View

from apps.poi.guards import AdminPOIManageMixin
from apps.poi import scoping

log = logging.getLogger(__name__)


def _room_has_poi(room):
    """True if at least one member of *room* is a POI."""
    for m in room.members.all():
        if getattr(m, "poi_profile", None) is not None:
            return True
    return False


def _full_name(u):
    try:
        return (u.get_full_name() or "").strip() or getattr(u, "email", "") or "User"
    except Exception:
        return str(u)


class POIChannelListView(AdminPOIManageMixin, View):
    """GET /poi/manage/rooms/  → channels that contain a POI, with members and
    which of them are 'extra' (added via extension vs. allowed contacts)."""

    def get(self, request):
        from apps.messaging.models import ChatRoom

        allowed_ids = set(scoping.allowed_contacts_qs().values_list("id", flat=True))
        rooms = (
            ChatRoom.objects.filter(is_archived=False)
            .prefetch_related("members")
            .order_by("-updated_at")
        )
        out = []
        for r in rooms:
            members = list(r.members.all())
            pois = [m for m in members if getattr(m, "poi_profile", None) is not None]
            if not pois:
                continue  # not a POI room
            extra_ids = scoping._room_extra_ids(r)
            member_rows = []
            for m in members:
                is_poi = getattr(m, "poi_profile", None) is not None
                member_rows.append({
                    "id": str(m.id),
                    "name": _full_name(m),
                    "is_poi": is_poi,
                    "is_allowed_contact": m.id in allowed_ids,
                    "is_extra": m.id in extra_ids,
                })
            out.append({
                "room_id": str(r.id),
                "name": r.name or "",
                "room_type": r.room_type,
                "members": member_rows,
            })
        return JsonResponse({"ok": True, "rooms": out})


class POIChannelAddStaffView(AdminPOIManageMixin, View):
    """POST /poi/manage/rooms/<room_id>/add-staff/  body: user_id
    Add a staffer to a POI channel and record the opt-in. Blocks adding a POI."""

    def post(self, request, room_id):
        from apps.messaging.models import ChatRoom, ChatRoomMember
        from apps.poi.integrations import get_user_model
        from apps.poi.models import POIRoomExtension

        room = get_object_or_404(ChatRoom, id=room_id)
        if not _room_has_poi(room):
            return JsonResponse(
                {"ok": False, "error": "This room does not contain a POI."},
                status=400,
            )

        User = get_user_model()
        user_id = (request.POST.get("user_id") or "").strip()
        target = User.objects.filter(id=user_id, is_active=True).first()
        if not target:
            return JsonResponse({"ok": False, "error": "Unknown user."}, status=404)

        # Hard block: never add another POI to a POI room.
        if getattr(target, "poi_profile", None) is not None:
            return JsonResponse(
                {"ok": False, "error": "A POI cannot be added to another POI's room."},
                status=403,
            )

        # Record the per-room opt-in, then add the membership.
        ext, _ = POIRoomExtension.objects.get_or_create(
            room=room, defaults={"extended_by": request.user}
        )
        ext.allowed_extra.add(target)
        ChatRoomMember.objects.get_or_create(
            room=room, user=target, defaults={"role": "member"}
        )

        log.info(
            "POI room %s extended: %s added %s",
            room.id, getattr(request.user, "id", "?"), target.id,
        )
        return JsonResponse({"ok": True, "user_id": str(target.id), "name": _full_name(target)})


class POIChannelRemoveStaffView(AdminPOIManageMixin, View):
    """POST /poi/manage/rooms/<room_id>/remove-staff/<user_id>/
    Remove a previously-added staffer and revoke the opt-in for them. Allowed
    contacts (admins/CEO) are not 'extra' and aren't removed here."""

    def post(self, request, room_id, user_id):
        from apps.messaging.models import ChatRoom, ChatRoomMember
        from apps.poi.models import POIRoomExtension

        room = get_object_or_404(ChatRoom, id=room_id)
        ext = POIRoomExtension.objects.filter(room=room).first()
        if ext:
            ext.allowed_extra.remove(*ext.allowed_extra.filter(id=user_id))
        # Remove their membership too so they lose room access immediately.
        ChatRoomMember.objects.filter(room=room, user_id=user_id).delete()

        log.info("POI room %s: removed extra staff %s", room.id, user_id)
        return JsonResponse({"ok": True})


class POIChannelRenameView(AdminPOIManageMixin, View):
    """POST /poi/manage/rooms/<room_id>/rename/
    Set a friendly name on a POI channel. Body: name=<text>."""

    MAX_LEN = 200

    def post(self, request, room_id):
        from apps.messaging.models import ChatRoom

        room = get_object_or_404(ChatRoom, id=room_id)
        if not _room_has_poi(room):
            return JsonResponse(
                {"ok": False, "error": "This room does not contain a POI."},
                status=400,
            )

        name = (request.POST.get("name") or "").strip()[: self.MAX_LEN]
        if not name:
            return JsonResponse(
                {"ok": False, "error": "Name cannot be empty."}, status=400
            )

        room.name = name
        room.save(update_fields=["name", "updated_at"])
        log.info("POI room %s renamed to %r by %s",
                 room.id, name, getattr(request.user, "id", "?"))
        return JsonResponse({"ok": True, "name": name})


class POIStaffSearchView(AdminPOIManageMixin, View):
    """GET /poi/manage/staff-search/?q=<text>&room_id=<uuid>
    Search active, non-POI users to add to a POI channel. Excludes anyone
    already a member of the given room. Returns at most 20 matches."""

    LIMIT = 20

    def get(self, request):
        from django.db.models import Q
        from apps.messaging.models import ChatRoom
        from apps.poi.integrations import get_user_model

        User = get_user_model()
        q = (request.GET.get("q") or "").strip()
        room_id = (request.GET.get("room_id") or "").strip()

        qs = User.objects.filter(is_active=True)

        # Never offer a POI as an addable staffer.
        qs = qs.exclude(poi_profile__isnull=False)

        # Exclude users already in the room.
        if room_id:
            room = ChatRoom.objects.filter(id=room_id).first()
            if room:
                member_ids = list(room.members.values_list("id", flat=True))
                qs = qs.exclude(id__in=member_ids)

        if q:
            filt = Q()
            for field in ("first_name", "last_name", "email", "username"):
                if _user_has_field(User, field):
                    filt |= Q(**{f"{field}__icontains": q})
            if filt:
                qs = qs.filter(filt)

        qs = qs.order_by("first_name", "last_name")[: self.LIMIT]
        data = [{"id": str(u.id), "name": _full_name(u),
                 "email": getattr(u, "email", "")} for u in qs]
        return JsonResponse({"ok": True, "results": data})


class POIChannelManageView(AdminPOIManageMixin, View):
    """GET /poi/manage/rooms/page/  → renders the staff-management HTML shell.
    The page itself calls the JSON endpoints above (list / search / add /
    remove) to do its work."""

    template_name = "poi/manage_rooms.html"

    def get(self, request):
        from django.shortcuts import render
        return render(request, self.template_name, {})


def _user_has_field(Model, name):
    try:
        Model._meta.get_field(name)
        return True
    except Exception:
        return False