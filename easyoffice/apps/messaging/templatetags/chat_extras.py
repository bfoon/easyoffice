"""
apps/messaging/templatetags/chat_extras.py
──────────────────────────────────────────
Template helpers for the chat UI.

Why this exists
---------------
A direct-message room's stored `name` contains BOTH people ("Ada Lovelace &
Bob Smith"), which is useless in your own sidebar — you already know you're
you. Every DM row should read as the other person's name.

Django templates can't assign a variable from inside a loop, so without a
filter the only way to reach the other member is to nest the whole row's
markup inside `{% for m in room.members.all %}{% if m.id != request.user.id %}`.
That triple-nests the markup, and it silently renders twice if a "direct"
room ever ends up with three members. One filter keeps both templates flat
and gives a single, predictable answer.

Usage
-----
    {% load chat_extras %}
    {% with other=item.room|dm_partner:request.user %}
      {{ other.full_name }}
    {% endwith %}

Falls back to `None` when there is no other member (a self-DM, or a room
whose second member was deleted), so templates should keep a `|default`
on the room name for that case.
"""

from django import template

register = template.Library()


@register.filter
def dm_partner(room, user):
    """
    The other member of a direct-message room, or None.

    Iterates `members.all()` rather than filtering in the database, so a
    view that prefetched members pays nothing extra. If your view does NOT
    prefetch, add it — otherwise this is one query per row:

        rooms = (ChatRoom.objects
                 .filter(members=request.user)
                 .prefetch_related('members'))
    """
    if room is None or user is None:
        return None

    user_id = getattr(user, 'id', None)
    if user_id is None:
        return None

    try:
        members = room.members.all()
    except Exception:
        return None

    for member in members:
        if member.id != user_id:
            return member
    return None


@register.filter
def room_display_name(room, user):
    """
    What to call a room in a list: the other person for a DM, otherwise the
    room's own name.

    Use this anywhere a room is named in a list — sidebar, forward picker,
    page title — so the naming stays consistent everywhere.
    """
    if room is None:
        return ''

    if getattr(room, 'room_type', '') == 'direct':
        other = dm_partner(room, user)
        if other is not None:
            name = (getattr(other, 'full_name', '') or '').strip()
            if name:
                return name
            return getattr(other, 'username', '') or (room.name or 'Direct Message')

    return room.name or 'Untitled room'
