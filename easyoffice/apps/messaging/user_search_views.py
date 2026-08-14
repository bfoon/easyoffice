"""
apps/messaging/user_search_views.py
──────────────────────────────────
🔎 Find a colleague and start a DM — from anywhere, including inside a room.

Why an endpoint rather than a template list
-------------------------------------------
chat_list.html renders every active user into the "Start a DM" modal and
filters them in JavaScript. That works at your current size, but:

  * chat_room.html's `all_staff` is the ADD-MEMBER list — people not already
    in this room. Reusing it for a DM picker would silently hide anyone who
    shares a channel with you, which is most of your colleagues.
  * Rendering the full staff directory into every chat page grows the HTML
    on every single room load, for a modal most visits never open.

So this searches server-side: one small request when the user actually types.
It also reports whether a DM already exists, so the picker can say "Open"
versus "Start a new conversation".

Nothing here creates anything. Picking a result navigates to the existing
`direct_message` view (/messages/dm/<user_id>/), which already opens or
creates the room. One code path for starting a DM, not two.
"""

import logging

from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import FieldDoesNotExist
from django.db.models import Q
from django.http import JsonResponse
from django.views.generic import View

from apps.core.models import User
from apps.messaging.models import ChatRoom

log = logging.getLogger(__name__)

# How many people come back per request. The picker is a keyboard list, not a
# directory browser — if the answer isn't in the top few, the query is too vague.
MAX_RESULTS = 12

# Fields we'll search if the User model actually has them. Checked against the
# model at import rather than assumed, so a project that renamed or dropped one
# gets a narrower search instead of a FieldError on every keystroke.
CANDIDATE_FIELDS = (
    'first_name', 'last_name', 'username', 'email',
    'full_name', 'staff_id', 'employee_id', 'phone',
)


def _searchable_fields():
    usable = []
    for name in CANDIDATE_FIELDS:
        try:
            User._meta.get_field(name)
        except FieldDoesNotExist:
            continue
        except Exception:
            continue
        usable.append(name)
    return usable


def _profile_select_related():
    """
    The deepest valid select_related path to the staff profile, or ().

    This MUST be resolved up front. `select_related('nope')` doesn't raise
    when you call it — it raises FieldError when the queryset is evaluated,
    deep inside rendering. A try/except around the .select_related() call
    catches nothing; the 500 lands on the user instead.
    """
    try:
        profile_field = User._meta.get_field('staffprofile')
    except Exception:
        return ()

    related = getattr(profile_field, 'related_model', None)
    if related is None:
        return ()

    for attr in ('position', 'department', 'unit'):
        try:
            related._meta.get_field(attr)
        except Exception:
            continue
        return ('staffprofile__' + attr,)

    return ('staffprofile',)


SEARCH_FIELDS = _searchable_fields()
PROFILE_SELECT_RELATED = _profile_select_related()


def _display_name(user):
    name = (getattr(user, 'full_name', '') or '').strip()
    if name:
        return name
    parts = [getattr(user, 'first_name', '') or '', getattr(user, 'last_name', '') or '']
    name = ' '.join(p for p in parts if p).strip()
    return name or getattr(user, 'username', '') or 'Unknown user'


def _initials(user):
    raw = (getattr(user, 'initials', '') or '').strip()
    if raw:
        return raw[:2].upper()
    name = _display_name(user)
    bits = [b for b in name.split() if b]
    if not bits:
        return '?'
    if len(bits) == 1:
        return bits[0][:1].upper()
    return (bits[0][:1] + bits[-1][:1]).upper()


def _avatar_url(user):
    try:
        if getattr(user, 'avatar', None) and user.avatar:
            return user.avatar.url
    except Exception:
        pass
    try:
        sp = getattr(user, 'staffprofile', None)
        if sp and getattr(sp, 'profile_picture', None) and sp.profile_picture:
            return sp.profile_picture.url
    except Exception:
        pass
    return ''


def _subtitle(user):
    """Job title and unit — what tells two people with the same name apart."""
    bits = []
    try:
        sp = getattr(user, 'staffprofile', None)
        if sp:
            position = getattr(sp, 'position', None)
            if position and getattr(position, 'title', ''):
                bits.append(position.title)
            for attr in ('department', 'unit'):
                obj = getattr(sp, attr, None)
                if obj:
                    label = getattr(obj, 'name', '') or str(obj)
                    if label and label not in bits:
                        bits.append(label)
                        break
    except Exception:
        pass
    if not bits:
        email = (getattr(user, 'email', '') or '').strip()
        if email:
            bits.append(email)
    return ' · '.join(bits)


def _existing_dm_map(me):
    """
    {other_user_id: room_id} for every DM I'm already in.

    Built in one pass instead of a lookup per result — a search that fires on
    every keystroke shouldn't issue a query per row.
    """
    out = {}
    try:
        rooms = (
            ChatRoom.objects
            .filter(room_type='direct', members=me)
            .prefetch_related('members')
        )
        for room in rooms:
            for member in room.members.all():
                if member.id != me.id:
                    out[str(member.id)] = str(room.id)
    except Exception:
        log.exception('user search: could not map existing DMs')
    return out


class UserSearchView(LoginRequiredMixin, View):
    """
    GET /messages/users/search/?q=fatou

    Response
        {
          "ok": true,
          "query": "fatou",
          "results": [
            {
              "id": "…", "name": "Fatou Njie", "initials": "FN",
              "avatar_url": "", "subtitle": "Accountant · Finance",
              "dm_url": "/messages/dm/<id>/",
              "existing_room_id": "…" | null
            }
          ]
        }

    An empty `q` returns suggestions — colleagues you have no DM with yet —
    so opening the picker is useful before typing anything.
    """

    def get(self, request):
        query = (request.GET.get('q') or '').strip()

        people = User.objects.filter(is_active=True).exclude(id=request.user.id)

        # Empty tuple when this project has no staff profile — the search
        # still works, it just costs a query per subtitle.
        if PROFILE_SELECT_RELATED:
            people = people.select_related(*PROFILE_SELECT_RELATED)

        if query:
            if not SEARCH_FIELDS:
                # Nothing indexable on the model; fall back to the display
                # name in Python rather than returning a confusing empty list.
                people = [u for u in people[:400]
                          if query.lower() in _display_name(u).lower()]
            else:
                condition = Q()
                for field in SEARCH_FIELDS:
                    condition |= Q(**{f'{field}__icontains': query})
                # A two-word query ("fatou njie") matches no single field, so
                # also try each word against every field.
                words = [w for w in query.split() if w]
                if len(words) > 1:
                    for word in words:
                        for field in SEARCH_FIELDS:
                            condition |= Q(**{f'{field}__icontains': word})
                people = people.filter(condition).distinct()

        existing = _existing_dm_map(request.user)

        if not query:
            # No query → suggest people you HAVEN'T spoken to. The ones you
            # have are already sitting in the sidebar; repeating them here
            # wastes the whole list.
            people = [u for u in people if str(u.id) not in existing]

        results = []
        for user in people:
            if len(results) >= MAX_RESULTS:
                break
            uid = str(user.id)
            results.append({
                'id': uid,
                'name': _display_name(user),
                'initials': _initials(user),
                'avatar_url': _avatar_url(user),
                'subtitle': _subtitle(user),
                'dm_url': f'/messages/dm/{uid}/',
                'existing_room_id': existing.get(uid),
            })

        # Someone you already talk to is the likelier target when you searched
        # for them by name; without a query we've excluded them entirely.
        if query:
            results.sort(key=lambda r: (r['existing_room_id'] is None, r['name'].lower()))
        else:
            results.sort(key=lambda r: r['name'].lower())

        return JsonResponse({'ok': True, 'query': query, 'results': results})
