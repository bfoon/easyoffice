"""
apps/messaging/memo.py
──────────────────────
Memos: a chat message that carries a SUBJECT LINE and a few email-style
affordances — priority, a reply-by date, an acknowledgement request, and
a Cc list that can optionally be delivered to real inboxes.

WHY A MEMO IS NOT A NEW MODEL
─────────────────────────────
A memo is stored as an ordinary ``ChatMessage``:

    message_type    = 'command'
    command_payload = {'command_type': 'memo', 'priority': …, …}
    content         = "<subject>\\n\\n<body>"

That buys three things for free, all of which a bespoke MemoMessage model
would have had to re-implement:

  • **Encryption.** ``content`` goes through ``EncryptedContentMixin``, so
    the subject and the body are both encrypted at rest exactly like every
    other message. A ``subject`` column on the model — or a subject sitting
    in the JSON payload — would have been stored in CLEAR TEXT next to an
    encrypted body, which is the sort of half-measure that reads as
    security but isn't. The subject is often the most sensitive line in a
    memo ("Disciplinary hearing — 14 Sept"), so it belongs inside the
    ciphertext.

  • **Every existing path.** Search, the sidebar preview, pinning,
    replies, forwarding, deletion, read receipts, history pagination and
    the serializer all keep working with no changes, because a memo is
    just a message.

  • **Subject-first previews.** Because the subject is the first line of
    ``content``, ``room.last_message.content[:60]`` shows the subject
    rather than a fragment of the body — which is what you'd want anyway.

``command_payload`` therefore holds only the parts that are metadata
rather than content: priority, the reply-by date, whether an
acknowledgement was requested, who was Cc'd, and who has acknowledged.

WIRING
──────
1.  urls.py::

        from apps.messaging import memo
        path('<uuid:room_id>/memo/create/',
             memo.CreateMemoView.as_view(), name='create_chat_memo'),
        path('<uuid:room_id>/memo/<uuid:message_id>/ack/',
             memo.AcknowledgeMemoView.as_view(), name='ack_chat_memo'),

2.  views.py — inside ``_serialize_chat_message``, where the command
    payload is attached::

        if msg.message_type == 'command':
            payload['command_payload'] = getattr(msg, 'command_payload', {}) or {}
            from apps.messaging import memo
            memo.decorate_payload(payload, msg, viewer)

3.  consumers.py — one more group-event handler::

        async def chat_memo(self, event):
            await self.send(text_data=json.dumps(event['payload']))

No migration is required.

SETTINGS
────────
    MESSAGING_MEMO_EMAIL_ENABLED = True
        Allows the sender to tick "also email this". Off means the tick
        box is ignored and a memo stays inside the app.

        ⚠️ Emailing a memo puts its subject and body through your mail
        relay and into recipients' mailboxes in plain text. That is a
        deliberate choice the SENDER makes per memo — it is the point of
        the feature — but it does mean at-rest encryption stops covering
        that copy. Set this to False if that trade is not acceptable for
        your deployment; the in-app memo still works.

    MESSAGING_MEMO_MAX_BODY = 5000
"""

import json
import logging
import re
import threading
from html.parser import HTMLParser

from django.conf import settings
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.html import escape
from django.views.generic import View

from apps.messaging.models import ChatMessage, ChatRoom, ChatRoomMember

log = logging.getLogger(__name__)

COMMAND_TYPE = 'memo'
PRIORITIES = ('low', 'normal', 'high')
SUBJECT_MAX = 140
DEFAULT_BODY_MAX = 5000

# Subject and body live in one encrypted field, split by the first blank
# line. Written only by build_content() and read only by split_content(),
# so the format never leaks into calling code.
_SEPARATOR = '\n\n'


# ═════════════════════════════════════════════════════════════════════════════
# 🖋️ RICH TEXT
# ─────────────────────────────────────────────────────────────────────────────
# A formatted memo body is stored as a small, strictly-defined subset of
# HTML. That subset is enforced HERE, on write, by an allowlist parser —
# never by the browser, and never by trusting what the composer sent.
#
# This matters more than it usually would. A memo body is rendered with
# innerHTML into every recipient's page, so anything that survives this
# function runs in their session. The rule is therefore: an element, an
# attribute or a CSS property that is not explicitly named below does not
# survive, and no value is passed through without being matched against a
# pattern. Dropping a tag keeps its TEXT — a memo never silently loses
# words, only formatting.
#
# bleach would do the same job; this is written by hand so the app gains
# no new dependency and the exact allowlist is readable in one screen.
# ═════════════════════════════════════════════════════════════════════════════

# Tags kept as tags. Everything else is unwrapped (its text is kept).
ALLOWED_TAGS = {
    'p', 'br', 'div', 'span',
    'b', 'strong', 'i', 'em', 'u', 's', 'strike', 'del', 'mark', 'sub', 'sup',
    'ul', 'ol', 'li', 'blockquote', 'code', 'pre', 'a', 'h4',
}

# Tags whose *content* is dropped too — text inside them is never wanted.
DROP_WITH_CONTENT = {'script', 'style', 'iframe', 'object', 'embed', 'svg',
                     'math', 'template', 'noscript', 'title'}

VOID_TAGS = {'br'}

# Attributes, per tag. 'style' is filtered property-by-property below.
ALLOWED_ATTRS = {
    '*': {'style'},
    'a': {'href', 'title'},
}

ALLOWED_STYLE_PROPS = {
    'color', 'background-color', 'font-family', 'font-size', 'font-weight',
    'font-style', 'text-decoration', 'text-decoration-line', 'text-align',
}

# Colours: hex or rgb()/rgba() only. Named colours are allowed from a short
# list — "red" is fine, but an arbitrary identifier is not, because that is
# where CSS-wide keywords and vendor oddities creep in.
_COLOR_NAMES = {
    'black', 'white', 'red', 'green', 'blue', 'yellow', 'orange', 'purple',
    'grey', 'gray', 'brown', 'pink', 'teal', 'navy', 'maroon', 'olive',
    'silver', 'lime', 'aqua', 'cyan', 'magenta', 'gold', 'transparent',
    'inherit', 'currentcolor',
}
_RE_HEX = re.compile(r'^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$')
_RE_RGB = re.compile(
    r'^rgba?\(\s*\d{1,3}\s*,\s*\d{1,3}\s*,\s*\d{1,3}\s*(?:,\s*(?:0|1|0?\.\d+)\s*)?\)$'
)
_RE_FONT_SIZE = re.compile(r'^(\d{1,3}(?:\.\d+)?)(px|pt|em|rem|%)$')
_FONT_SIZE_BOUNDS = {          # (min, max) per unit — a memo is a message,
    'px':  (8, 48),            # not a poster. Anything outside is dropped
    'pt':  (6, 36),            # rather than clamped, so the composer's own
    'em':  (0.5, 3),           # presets always land inside the range.
    'rem': (0.5, 3),
    '%':   (50, 300),
}
_RE_FONT_WEIGHT = re.compile(r'^(?:normal|bold|bolder|lighter|[1-9]00)$')

# Font families are matched against a fixed list rather than pattern-checked:
# a family name is free text, and free text in a style attribute is how
# url() and expression() get in.
DEFAULT_FONTS = [
    'Arial', 'Helvetica', 'Georgia', 'Times New Roman', 'Courier New',
    'Verdana', 'Tahoma', 'Trebuchet MS', 'Calibri', 'Cambria',
    'Segoe UI', 'Roboto', 'Inter', 'system-ui', 'monospace', 'serif',
    'sans-serif',
]

_DANGEROUS = re.compile(r'(?:url\s*\(|expression\s*\(|javascript\s*:|/\*|\*/|@import|&#)', re.I)

MAX_HTML_BYTES = 200_000       # a sane ceiling on one memo's markup
MAX_NESTING = 20


def allowed_fonts():
    fonts = getattr(settings, 'MESSAGING_MEMO_FONTS', None) or DEFAULT_FONTS
    return [str(f) for f in fonts]


def _font_family_ok(value):
    """True when every family in the stack is one we publish."""
    known = {f.lower() for f in allowed_fonts()}
    known |= {'serif', 'sans-serif', 'monospace', 'cursive', 'system-ui', 'inherit'}
    for part in value.split(','):
        name = part.strip().strip('\'"').lower()
        if not name or name not in known:
            return False
    return True


def _style_value_ok(prop, value):
    value = value.strip()
    if not value or len(value) > 120 or _DANGEROUS.search(value):
        return False

    if prop in ('color', 'background-color'):
        v = value.lower()
        return bool(_RE_HEX.match(value) or _RE_RGB.match(v) or v in _COLOR_NAMES)
    if prop == 'font-family':
        return _font_family_ok(value)
    if prop == 'font-size':
        match = _RE_FONT_SIZE.match(value)
        if match:
            try:
                number = float(match.group(1))
            except ValueError:
                return False
            low, high = _FONT_SIZE_BOUNDS[match.group(2)]
            return low <= number <= high
        return value.lower() in (
            'small', 'medium', 'large', 'x-large', 'smaller', 'larger')
    if prop == 'font-weight':
        return bool(_RE_FONT_WEIGHT.match(value.lower()))
    if prop == 'font-style':
        return value.lower() in ('normal', 'italic', 'oblique')
    if prop in ('text-decoration', 'text-decoration-line'):
        parts = value.lower().split()
        return bool(parts) and all(
            p in ('underline', 'line-through', 'overline', 'none') for p in parts)
    if prop == 'text-align':
        return value.lower() in ('left', 'right', 'center', 'justify')
    return False


def _clean_style(raw):
    """Filter a style attribute down to the properties we publish."""
    kept = []
    for declaration in (raw or '').split(';'):
        if ':' not in declaration:
            continue
        prop, _, value = declaration.partition(':')
        prop = prop.strip().lower()
        if prop not in ALLOWED_STYLE_PROPS:
            continue
        if _style_value_ok(prop, value):
            kept.append(f'{prop}:{value.strip()}')
    return ';'.join(kept)


def _clean_href(raw):
    href = (raw or '').strip()
    if not href or len(href) > 2000:
        return ''
    low = href.lower().replace('\t', '').replace('\n', '').replace('\r', '')
    if low.startswith(('http://', 'https://', 'mailto:', '/')):
        return href
    return ''      # javascript:, data:, vbscript:, protocol-relative, …


class _Sanitizer(HTMLParser):
    """Allowlist rewriter. Unknown tags are unwrapped, their text kept."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out = []
        self.open_stack = []
        self.skip_depth = 0          # inside <script> etc.

    # ── helpers ──────────────────────────────────────────────────────────
    def _emit(self, text):
        self.out.append(text)

    def _attrs_for(self, tag, attrs):
        allowed = ALLOWED_ATTRS.get('*', set()) | ALLOWED_ATTRS.get(tag, set())
        rendered = []
        for name, value in attrs:
            name = (name or '').lower()
            if name not in allowed:
                continue
            if name == 'style':
                cleaned = _clean_style(value)
                if cleaned:
                    rendered.append(f'style="{escape(cleaned)}"')
            elif name == 'href':
                href = _clean_href(value)
                if href:
                    rendered.append(f'href="{escape(href)}"')
            elif name == 'title':
                rendered.append(f'title="{escape((value or "")[:200])}"')
        if tag == 'a':
            if not any(r.startswith('href=') for r in rendered):
                return None          # a link with no usable target is just text
            # Never hand a memo link opener-access to the chat window.
            rendered.append('target="_blank"')
            rendered.append('rel="noopener noreferrer nofollow"')
        return rendered

    # ── parser callbacks ─────────────────────────────────────────────────
    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if self.skip_depth or tag in DROP_WITH_CONTENT:
            self.skip_depth += 1
            return
        if tag not in ALLOWED_TAGS or len(self.open_stack) >= MAX_NESTING:
            return                    # unwrap: children still get emitted
        rendered = self._attrs_for(tag, attrs)
        if rendered is None:
            return
        if tag in VOID_TAGS:
            self._emit(f'<{tag}>')
            return
        self.open_stack.append(tag)
        self._emit('<' + tag + (' ' + ' '.join(rendered) if rendered else '') + '>')

    def handle_startendtag(self, tag, attrs):
        tag = tag.lower()
        if self.skip_depth:
            return
        if tag in VOID_TAGS:
            self._emit(f'<{tag}>')

    def handle_endtag(self, tag):
        tag = tag.lower()
        if self.skip_depth:
            self.skip_depth -= 1
            return
        if tag in VOID_TAGS or tag not in ALLOWED_TAGS:
            return
        if tag in self.open_stack:
            # Close everything opened inside it too, so a stray </p> can't
            # leave the document unbalanced.
            while self.open_stack:
                open_tag = self.open_stack.pop()
                self._emit(f'</{open_tag}>')
                if open_tag == tag:
                    break

    def handle_data(self, data):
        if self.skip_depth:
            return
        self._emit(escape(data))

    def handle_comment(self, data):
        pass

    def handle_decl(self, decl):
        pass

    def unknown_decl(self, data):
        pass

    def result(self):
        while self.open_stack:
            self._emit(f'</{self.open_stack.pop()}>')
        return ''.join(self.out)


def sanitize_html(html: str) -> str:
    """
    Return *html* reduced to the memo formatting subset.

    Safe to call on anything, including markup this app never produced —
    a paste out of Word, or a hand-crafted request body.
    """
    if not html:
        return ''
    if len(html) > MAX_HTML_BYTES:
        html = html[:MAX_HTML_BYTES]
    parser = _Sanitizer()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        log.exception('memo: sanitiser failed — falling back to plain text')
        return escape(strip_html(html))
    return parser.result().strip()


_RE_TAG = re.compile(r'<[^>]+>')
_RE_BLOCK_BREAK = re.compile(r'</\s*(?:p|div|li|blockquote|h4|pre)\s*>|<\s*br\s*/?>', re.I)


def strip_html(html: str) -> str:
    """
    Plain-text rendering of a memo body, for previews, notifications,
    mention parsing and the clipboard. Block ends become newlines so the
    text doesn't run together.
    """
    if not html:
        return ''
    import html as _html
    text = _RE_BLOCK_BREAK.sub('\n', html)
    text = _RE_TAG.sub('', text)
    text = _html.unescape(text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def looks_like_html(value: str) -> bool:
    return bool(value) and bool(re.search(r'<(?:/?[a-zA-Z]+)[^>]*>', value))


# ─────────────────────────────────────────────────────────────────────────────
# Content packing
# ─────────────────────────────────────────────────────────────────────────────

def build_content(subject: str, body: str) -> str:
    """Pack a subject and body into the single encrypted content field."""
    subject = ' '.join((subject or '').split())        # force to one line
    return f'{subject}{_SEPARATOR}{body or ""}'


def split_content(content: str):
    """Unpack ``content`` into ``(subject, body)``. Tolerant of junk."""
    text = (content or '').replace('\r\n', '\n')
    if _SEPARATOR in text:
        subject, body = text.split(_SEPARATOR, 1)
    else:
        # A memo whose body was emptied by an edit, or an old row — treat
        # the first line as the subject and keep whatever follows.
        parts = text.split('\n', 1)
        subject = parts[0]
        body = parts[1] if len(parts) > 1 else ''
    return subject.strip(), body.strip('\n')


def is_memo(msg) -> bool:
    if getattr(msg, 'message_type', '') != 'command':
        return False
    payload = getattr(msg, 'command_payload', None) or {}
    return payload.get('command_type') == COMMAND_TYPE


# ─────────────────────────────────────────────────────────────────────────────
# Read side
# ─────────────────────────────────────────────────────────────────────────────

def memo_view_payload(msg, viewer=None) -> dict:
    """
    Everything a client needs to draw a memo card.

    Acknowledgement counts are computed against CURRENT room membership,
    not against the membership at send time, so somebody who left the room
    can't hold a memo at "3 of 4 acknowledged" forever.
    """
    payload = getattr(msg, 'command_payload', None) or {}
    subject, body = split_content(msg.content or '')

    acks = payload.get('acks') or {}
    member_ids = set()
    try:
        member_ids = {
            str(uid) for uid in
            ChatRoomMember.objects.filter(room_id=msg.room_id)
            .exclude(user_id=msg.sender_id)
            .values_list('user_id', flat=True)
        }
    except Exception:
        log.exception('memo: could not read room membership for %s', msg.pk)

    live_acks = {uid: at for uid, at in acks.items() if uid in member_ids}
    viewer_id = str(getattr(viewer, 'id', '') or '')

    priority = payload.get('priority') or 'normal'
    if priority not in PRIORITIES:
        priority = 'normal'

    # Older memos predate rich text and have no marker; anything without
    # one is plain text, which the client escapes. Re-sanitising on READ
    # as well as write costs little and means a body that somehow got into
    # the database another way still can't put script into a page.
    body_format = payload.get('body_format')
    if body_format != 'html':
        body_format = 'html' if looks_like_html(body) and payload.get('body_format') else 'text'
    if body_format == 'html':
        body = sanitize_html(body)

    return {
        'command_type':   COMMAND_TYPE,
        'message_id':     str(msg.pk),
        'subject':        subject,
        'body':           body,
        'body_format':    body_format,
        'body_text':      strip_html(body) if body_format == 'html' else body,
        'priority':       priority,
        'due':            payload.get('due') or '',
        'ack_requested':  bool(payload.get('ack_requested')),
        'cc':             payload.get('cc') or [],
        'emailed_to':     int(payload.get('emailed_to') or 0),
        'ack_count':      len(live_acks),
        'ack_total':      len(member_ids),
        'acked_by_me':    viewer_id in live_acks,
        'acked_by':       [
            {'user_id': uid, 'at': at}
            for uid, at in sorted(live_acks.items(), key=lambda kv: kv[1])
        ],
    }


def decorate_payload(payload: dict, msg, viewer=None) -> dict:
    """
    Hook for ``_serialize_chat_message``: fold the memo view data into the
    command payload that already goes over the wire, so the client needs
    no second request to draw the card.
    """
    try:
        if is_memo(msg):
            payload.setdefault('command_payload', {})
            payload['command_payload'] = dict(payload['command_payload'] or {})
            payload['command_payload'].update(memo_view_payload(msg, viewer))
            # The subject is the useful preview; the raw content is
            # "subject\n\nbody" which reads oddly in a notification.
            payload['memo'] = payload['command_payload']
    except Exception:
        log.exception('memo: decorate_payload failed for %s', getattr(msg, 'pk', '?'))
    return payload


def memo_json(msg, viewer=None) -> str:
    """JSON blob for a server-rendered mount point (see memo_extras.py)."""
    try:
        return json.dumps(memo_view_payload(msg, viewer))
    except Exception:
        log.exception('memo: memo_json failed for %s', getattr(msg, 'pk', '?'))
        return '{}'


# ─────────────────────────────────────────────────────────────────────────────
# Write side
# ─────────────────────────────────────────────────────────────────────────────

def _body_max():
    try:
        return int(getattr(settings, 'MESSAGING_MEMO_MAX_BODY', DEFAULT_BODY_MAX))
    except (TypeError, ValueError):
        return DEFAULT_BODY_MAX


class MemoError(ValueError):
    """A memo the user needs to fix before it can be sent."""


def create_memo(room, sender, *, subject, body, priority='normal', due='',
                ack_requested=False, cc_users=None, reply_to=None,
                body_format='text'):
    """
    Create and persist a memo message. Returns the ChatMessage.

    ``body_format`` is 'html' for a formatted body (bold, colour, fonts)
    or 'text' for plain. HTML is passed through sanitize_html() here —
    this is the ONLY write path into a memo body, and EditMessageView
    refuses non-text messages, so nothing can reach a recipient's page
    without going through the allowlist.

    Raises MemoError with a user-facing message on bad input.
    """
    subject = ' '.join((subject or '').split())
    body = body or ''

    body_format = 'html' if body_format == 'html' else 'text'
    if body_format == 'html':
        body = sanitize_html(body)
        plain = strip_html(body)
    else:
        body = body.strip()
        plain = body

    if not subject:
        raise MemoError('A memo needs a subject.')
    if not plain.strip():
        raise MemoError('A memo needs a message.')
    if len(subject) > SUBJECT_MAX:
        subject = subject[:SUBJECT_MAX]
    if len(plain) > _body_max():
        # Measure the limit against readable text, not markup — otherwise
        # a heavily formatted memo hits the ceiling far sooner than a
        # plain one of the same length, which reads as a bug.
        raise MemoError(
            f'That memo is too long ({len(plain)} characters; '
            f'the limit is {_body_max()}).'
        )

    priority = (priority or 'normal').lower()
    if priority not in PRIORITIES:
        priority = 'normal'

    due = (due or '').strip()
    if due:
        # Store only a value we can render back; anything else is dropped
        # rather than shown to recipients as a broken date.
        try:
            from datetime import date
            date.fromisoformat(due)
        except Exception:
            due = ''

    cc = []
    for user in (cc_users or []):
        cc.append({
            'user_id': str(user.pk),
            'name': _full_name(user),
        })

    msg = ChatMessage.objects.create(
        room=room,
        sender=sender,
        message_type='command',
        content=build_content(subject, body),
        reply_to=reply_to,
        command_payload={
            'command_type':  COMMAND_TYPE,
            'priority':      priority,
            'due':           due,
            'ack_requested': bool(ack_requested),
            'body_format':   body_format,
            'cc':            cc,
            'acks':          {},
            'emailed_to':    0,
            'sent_at':       timezone.now().isoformat(),
        },
    )
    return msg


def acknowledge(msg, user):
    """
    Record ``user``'s acknowledgement of a memo. Idempotent.

    Runs under select_for_update because two people acknowledging at the
    same moment would otherwise read the same JSON, each add their own
    entry, and the second write would drop the first.
    """
    with transaction.atomic():
        row = ChatMessage.objects.select_for_update().get(pk=msg.pk)
        payload = dict(row.command_payload or {})
        acks = dict(payload.get('acks') or {})
        uid = str(user.pk)
        if uid not in acks:
            acks[uid] = timezone.now().isoformat()
            payload['acks'] = acks
            row.command_payload = payload
            # update_fields keeps the encrypted content column untouched.
            row.save(update_fields=['command_payload'])
        return row


# ─────────────────────────────────────────────────────────────────────────────
# Email delivery
# ─────────────────────────────────────────────────────────────────────────────

def _full_name(user):
    try:
        from apps.messaging.views import _safe_full_name
        return _safe_full_name(user)
    except Exception:
        return str(user)


def _email_enabled():
    return bool(getattr(settings, 'MESSAGING_MEMO_EMAIL_ENABLED', True))


def _memo_email_html(*, subject, body, body_format, sender_name, room_name,
                     priority, due, ack_requested, org_name, chat_url):
    """
    Every interpolated value is escaped — this is assembled as HTML and
    lands in mail clients, so an unescaped body would be an injection
    vector into recipients' inboxes.

    The one exception is a formatted body, which is inserted as markup
    because that is the whole point of the feature. It is safe to do so
    ONLY because it went through sanitize_html() on the way into the
    database — it is not user input at this point, it is allowlisted
    output. Mail clients strip most of what survives anyway.
    """
    prio_row = ''
    if priority == 'high':
        prio_row = (
            '<tr><td style="padding:2px 0;color:#b91c1c;font-weight:700;'
            'font-size:13px">High priority</td></tr>'
        )
    elif priority == 'low':
        prio_row = (
            '<tr><td style="padding:2px 0;color:#64748b;font-size:13px">'
            'Low priority / FYI</td></tr>'
        )

    due_row = ''
    if due:
        due_row = (
            '<tr><td style="padding:2px 0;color:#334155;font-size:13px">'
            'Reply by <strong>' + escape(due) + '</strong></td></tr>'
        )

    ack_row = ''
    if ack_requested:
        ack_row = (
            '<tr><td style="padding:2px 0;color:#334155;font-size:13px">'
            'An acknowledgement was requested — open the chat to confirm.'
            '</td></tr>'
        )

    if body_format == 'html':
        body_html = body
    else:
        body_html = escape(body).replace('\n', '<br>')

    return f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"/></head>
<body style="margin:0;padding:0;background:#f1f5f9;font-family:'Segoe UI',Arial,sans-serif;">
<div style="max-width:620px;margin:28px auto;background:#fff;border-radius:14px;overflow:hidden;box-shadow:0 4px 24px rgba(0,0,0,.08)">
  <div style="background:linear-gradient(135deg,#1e3a5f,#6366f1);padding:24px 34px">
    <div style="font-size:11px;letter-spacing:.12em;text-transform:uppercase;color:rgba(255,255,255,.75);font-weight:700">Memo</div>
    <h1 style="margin:6px 0 0;font-size:19px;color:#fff;font-weight:700;line-height:1.35">{escape(subject)}</h1>
  </div>
  <div style="padding:24px 34px">
    <table style="border-collapse:collapse;margin-bottom:18px">
      <tr><td style="padding:2px 0;color:#334155;font-size:13px">
        From <strong>{escape(sender_name)}</strong> in <strong>{escape(room_name)}</strong>
      </td></tr>
      {prio_row}
      {due_row}
      {ack_row}
    </table>
    <div style="border-left:4px solid #6366f1;background:#f8fafc;border-radius:0 8px 8px 0;padding:16px 20px;font-size:14px;color:#1e293b;line-height:1.65;margin-bottom:24px">
      {body_html}
    </div>
    <div style="text-align:center">
      <a href="{escape(chat_url)}" style="display:inline-block;background:linear-gradient(135deg,#6366f1,#4f46e5);color:#fff;padding:12px 32px;border-radius:10px;text-decoration:none;font-weight:700;font-size:14px">
        Open in {escape(org_name)}
      </a>
    </div>
  </div>
  <div style="background:#f8fafc;padding:16px 34px;border-top:1px solid #e2e8f0;font-size:11px;color:#94a3b8;text-align:center">
    {escape(org_name)} · Sent as a memo from a chat conversation. Reply in the app so the thread stays in one place.
  </div>
</div>
</body></html>"""


def email_memo(msg, room, sender, recipients):
    """
    Mail a memo to *recipients* in a background thread.

    Returns immediately with the number of addressable recipients; the
    actual sending is fire-and-forget so a slow SMTP server never holds
    the composer open.
    """
    if not _email_enabled():
        return 0

    targets = [r for r in recipients if getattr(r, 'email', '')]
    if not targets:
        return 0

    subject, body = split_content(msg.content or '')
    payload = msg.command_payload or {}

    org_name = getattr(settings, 'ORGANISATION_NAME',
                       getattr(settings, 'OFFICE_NAME', 'EasyOffice'))
    from_email = getattr(settings, 'DEFAULT_FROM_EMAIL',
                         f'noreply@{org_name.lower().replace(" ", "")}.org')
    chat_url = (getattr(settings, 'SITE_URL', '') or '').rstrip('/') + \
        f'/messages/{room.id}/'
    sender_name = _full_name(sender)
    room_name = room.name or 'a chat'

    html = _memo_email_html(
        subject=subject, body=body,
        body_format=(payload.get('body_format') or 'text'),
        sender_name=sender_name,
        room_name=room_name, priority=payload.get('priority') or 'normal',
        due=payload.get('due') or '',
        ack_requested=bool(payload.get('ack_requested')),
        org_name=org_name, chat_url=chat_url,
    )

    def _send():
        from django.core.mail import EmailMessage
        for recipient in targets:
            try:
                mail = EmailMessage(
                    subject=f'[Memo] {subject}',
                    body=html,
                    from_email=from_email,
                    to=[recipient.email],
                )
                mail.content_subtype = 'html'
                mail.send()
            except Exception:
                log.exception('memo: email to %s failed', recipient.pk)

    threading.Thread(target=_send, daemon=True).start()
    return len(targets)


def _suppress_generic_notification(room, sender, recipients):
    """
    Stop a memo recipient getting the generic "you have a new message"
    email seconds after the memo itself. _send_offline_message_notification
    rate-limits on this exact cache key, so claiming it first makes that
    helper skip these people without changing its code.
    """
    try:
        from django.core.cache import cache
        for r in recipients:
            cache.set(f'msg_notif:{r.pk}:{room.id}:{sender.pk}', True, timeout=600)
    except Exception:
        log.exception('memo: could not suppress duplicate notification')


# ─────────────────────────────────────────────────────────────────────────────
# Broadcast
# ─────────────────────────────────────────────────────────────────────────────

def broadcast_memo_update(msg, viewer=None):
    """Push refreshed ack state to every open client in the room."""
    try:
        from asgiref.sync import async_to_sync
        from channels.layers import get_channel_layer

        layer = get_channel_layer()
        if layer is None:
            return
        async_to_sync(layer.group_send)(
            f'chat_{msg.room_id}',
            {
                'type': 'chat.memo',
                'payload': {
                    'type': 'memo_update',
                    'room_id': str(msg.room_id),
                    'message_id': str(msg.pk),
                    'memo': memo_view_payload(msg, viewer),
                },
            },
        )
    except Exception:
        log.exception('memo: broadcast failed for %s', msg.pk)


# ─────────────────────────────────────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────────────────────────────────────

class CreateMemoView(LoginRequiredMixin, View):
    """
    POST /messages/<room_id>/memo/create/

    Form fields:
        subject   required, one line
        body      required
        priority  low | normal | high      (default normal)
        due       YYYY-MM-DD               (optional)
        ack       '1' to request acknowledgement
        cc[]      user ids, repeated       (optional)
        email     '1' to also send it to inboxes
        reply_to  message uuid             (optional)

    Response:
        {ok: true, payload: {…serialized message…}, emailed: 3}
    """

    def post(self, request, room_id):
        room = get_object_or_404(ChatRoom, id=room_id, members=request.user)

        try:
            from apps.messaging.views import _can_post_in_room
            can_post = _can_post_in_room(request.user, room)
        except Exception:
            can_post = not room.is_readonly
        if not can_post:
            return JsonResponse(
                {'ok': False, 'error': 'You cannot post in this room.'},
                status=403)

        cc_ids = [c for c in request.POST.getlist('cc[]') if c]
        cc_users = []
        if cc_ids:
            # Only ever Cc people who are actually in the room — otherwise
            # this endpoint becomes a way to email arbitrary users.
            cc_users = list(room.members.filter(id__in=cc_ids)
                            .exclude(id=request.user.id))

        reply_obj = None
        reply_to_id = (request.POST.get('reply_to') or '').strip()
        if reply_to_id:
            reply_obj = ChatMessage.objects.filter(
                id=reply_to_id, room=room, is_deleted=False).first()

        try:
            msg = create_memo(
                room, request.user,
                subject=request.POST.get('subject', ''),
                body=request.POST.get('body', ''),
                body_format=request.POST.get('body_format', 'text'),
                priority=request.POST.get('priority', 'normal'),
                due=request.POST.get('due', ''),
                ack_requested=request.POST.get('ack') == '1',
                cc_users=cc_users,
                reply_to=reply_obj,
            )
        except MemoError as exc:
            return JsonResponse({'ok': False, 'error': str(exc)}, status=400)

        room.updated_at = timezone.now()
        room.save(update_fields=['updated_at'])
        ChatRoomMember.objects.filter(room=room, user=request.user).update(
            last_read=timezone.now())

        emailed = 0
        if request.POST.get('email') == '1':
            recipients = cc_users or list(
                room.members.exclude(id=request.user.id))
            emailed = email_memo(msg, room, request.user, recipients)
            if emailed:
                _suppress_generic_notification(room, request.user, recipients)
                payload = dict(msg.command_payload or {})
                payload['emailed_to'] = emailed
                msg.command_payload = payload
                msg.save(update_fields=['command_payload'])

        from apps.messaging.views import (
            _serialize_chat_message, _broadcast_chat_message,
            _notify_offline_members, _save_mentions,
        )

        try:
            _save_mentions(msg)
        except Exception:
            log.exception('memo: mention save failed')

        _broadcast_chat_message(msg, viewer=request.user)

        try:
            _notify_offline_members(room, request.user, msg)
        except Exception:
            log.exception('memo: offline notify failed')

        return JsonResponse({
            'ok': True,
            'emailed': emailed,
            'email_enabled': _email_enabled(),
            'payload': _serialize_chat_message(msg, viewer=request.user),
        })


class AcknowledgeMemoView(LoginRequiredMixin, View):
    """
    POST /messages/<room_id>/memo/<message_id>/ack/

    Idempotent. Returns the refreshed memo view payload.
    """

    def post(self, request, room_id, message_id):
        room = get_object_or_404(ChatRoom, id=room_id, members=request.user)
        msg = get_object_or_404(ChatMessage, id=message_id, room=room,
                                is_deleted=False)

        if not is_memo(msg):
            return JsonResponse(
                {'ok': False, 'error': 'That message is not a memo.'},
                status=400)
        if msg.sender_id == request.user.id:
            return JsonResponse(
                {'ok': False, 'error': 'You wrote this memo.'}, status=400)

        msg = acknowledge(msg, request.user)
        broadcast_memo_update(msg, viewer=request.user)

        return JsonResponse({
            'ok': True,
            'memo': memo_view_payload(msg, viewer=request.user),
        })