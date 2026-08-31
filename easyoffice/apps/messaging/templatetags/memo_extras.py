"""
apps/messaging/templatetags/memo_extras.py
──────────────────────────────────────────
One filter, used by chat_room.html to hand a server-rendered memo to the
client as a JSON mount point:

    {% load memo_extras %}
    <div class="eo-memo-mount" data-memo="{{ msg|memo_data:request.user }}"></div>

Why a filter rather than fields on the message: a memo's subject and body
live inside the ENCRYPTED content field (see apps/messaging/memo.py), so
a template cannot pull them apart with dotted attribute access. Doing the
split here keeps that format in exactly one place — the memo module —
instead of spreading it across the template.

The value is JSON. Django autoescapes it into the attribute, and the
browser un-escapes it before JSON.parse sees it, so no |safe is needed
and none should be added.
"""

from django import template

register = template.Library()


@register.filter
def memo_data(message, viewer=None):
    """Return the memo view payload for *message* as a JSON string."""
    from apps.messaging import memo
    return memo.memo_json(message, viewer)


@register.filter
def is_memo(message):
    """True when this ChatMessage is a memo."""
    from apps.messaging import memo
    return memo.is_memo(message)
