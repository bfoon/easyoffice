"""
apps/customer_service/templatetags/portal_tags.py
==================================================

Template tags that bridge the staff-side ticket detail UI to the
customer portal. The headline tag is `portal_reply_button` — drop it
into your activity log loop and it renders an inline "Reply" affordance
on rows that originated from the customer portal.

Setup:
  1. Make sure this file lives at:
       apps/customer_service/templatetags/portal_tags.py
  2. Make sure there is an empty `__init__.py` at:
       apps/customer_service/templatetags/__init__.py
  3. Load the library in your template:
       {% load portal_tags %}

Usage in your existing CS ticket detail template:

    {% load portal_tags %}

    {% for u in ticket.updates.all %}
      <div class="activity-row">
        <span class="activity-author">{{ u.user|default:"System" }}</span>
        <span class="activity-type">{{ u.get_update_type_display }}</span>
        <span class="activity-time">{{ u.created_at }}</span>
        <div class="activity-body">{{ u.body|linebreaksbr }}</div>

        {# This is the new bit: #}
        {% portal_reply_button u ticket %}
      </div>
    {% endfor %}

The tag renders nothing for non-portal updates, so it's safe to drop in
unconditionally.
"""
from __future__ import annotations

from django import template

register = template.Library()


# ─────────────────────────────────────────────────────────────────────────────
# Detection: is this update something a customer wrote on the portal?
# ─────────────────────────────────────────────────────────────────────────────

# customer_portal.sync.mirror_customer_comment_to_staff stamps every
# customer reply's body with this prefix. We use it as the marker.
_CUSTOMER_COMMENT_PREFIX = '💬 Reply from'


@register.filter(name='is_portal_customer_comment')
def is_portal_customer_comment(update) -> bool:
    """True if a ServiceTicketUpdate row was written by the portal sync layer."""
    if not update or not getattr(update, 'body', None):
        return False
    return update.body.startswith(_CUSTOMER_COMMENT_PREFIX)


@register.filter(name='extract_portal_message')
def extract_portal_message(update) -> str:
    """
    Pull just the customer's actual message body out of the wrapped
    'Reply from X (via portal):\\n\\n…' format. Useful if you want to
    render the message without the wrapper line.
    """
    if not is_portal_customer_comment(update):
        return getattr(update, 'body', '') or ''

    body = update.body
    # Strip the first line (the "Reply from X" header) and any
    # immediately-following blank line.
    lines = body.split('\n')
    if lines and lines[0].startswith(_CUSTOMER_COMMENT_PREFIX):
        lines = lines[1:]
    if lines and not lines[0].strip():
        lines = lines[1:]
    return '\n'.join(lines).strip()


# ─────────────────────────────────────────────────────────────────────────────
# The reply button (inclusion tag)
# ─────────────────────────────────────────────────────────────────────────────

@register.inclusion_tag(
    'customer_service/_portal_reply_button.html',
    takes_context=True,
)
def portal_reply_button(context, update, ticket):
    """
    Render an inline "Reply" button + collapsed reply form for a
    ServiceTicketUpdate row that came from the customer portal.

    Renders nothing for non-portal updates, so it's safe to call on
    every row in your activity log loop.

    Usage:
        {% portal_reply_button u ticket %}
    """
    is_customer = is_portal_customer_comment(update)

    # Only show the button if there's a portal request to reply to —
    # i.e. the ticket has a linked PortalSupportRequest and that
    # request is not in a terminal state.
    can_reply = False
    if is_customer:
        portal_req = (
            getattr(ticket, 'portal_request_origin', None)
            and ticket.portal_request_origin.first()
        )
        if portal_req and portal_req.status not in ('cancelled', 'closed'):
            can_reply = True

    return {
        'update': update,
        'ticket': ticket,
        'is_customer': is_customer,
        'can_reply': can_reply,
        'request': context.get('request'),
        # Used to build a stable id for the toggle target.
        'reply_id': f'portal-reply-{getattr(update, "pk", "x")}',
    }