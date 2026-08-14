"""
apps/messaging/calendar_views.py
────────────────────────────────
📅 CALENDAR + MEETING INVITES INSIDE CHAT  (Outlook-style)

What this gives you
-------------------
1. A calendar panel in the chat page (month grid + agenda list).
2. "New meeting" from the composer → creates a real `Meeting` in the
   meetings app AND drops an interactive invite card into the room.
3. The invite card carries Accept / Tentative / Decline buttons. One click
   writes the RSVP onto `MeetingAttendee` — which is what the calendar
   reads from, so accepting *is* "adding it to my calendar".
4. Everyone in the room sees the RSVP tally update live over the existing
   chat WebSocket.
5. Reminders: clients poll `/messages/calendar/reminders/` and get back any
   meeting that is inside its lead time. The reminder stays until the user
   dismisses or snoozes it, exactly like Outlook's reminder window.
6. A scheduling assistant: free/busy for the people you're inviting, plus
   suggested times when everyone is actually free.

Design notes
------------
* ZERO NEW MODELS, ZERO MIGRATIONS.
  - The invite card is a normal `ChatMessage` with
    `message_type='command'` and `command_payload.command_type='meeting_invite'`.
    That reuses the exact plumbing polls and task-cards already ride on.
  - RSVP state lives on `MeetingAttendee` (already has accepted / tentative /
    declined), so the chat card and the /meetings/ pages can never disagree.
  - Reminder lead time and dismissals live in the cache. Losing them is
    harmless: lead time falls back to 15 minutes, a dismissal reappears once.
    If you'd rather have them durable, see "Optional hardening" at the
    bottom of INTEGRATION.md.

* The card in the message stream is rendered CLIENT-side from live data
  (`ChatMeetingStateView`), not from the frozen `command_payload`. The
  payload is only a fallback label for search results, notification
  previews, and any client that can't run the hydrator.
"""

import json
import logging
from datetime import datetime, timedelta, time as dtime

from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.cache import cache
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.views.generic import View

from apps.core.models import User
from apps.messaging.models import ChatRoom, ChatRoomMember, ChatMessage
from apps.meetings.models import Meeting, MeetingAttendee

from apps.messaging.views import (
    _broadcast_chat_message,
    _can_post_in_room,
    _notify_offline_members,
    _room_group_name,
    _safe_full_name,
    _safe_initials,
    _serialize_chat_message,
)

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Tunables
# ─────────────────────────────────────────────────────────────────────────────

DEFAULT_REMINDER_MINUTES = 15
REMINDER_CHOICES = (0, 5, 10, 15, 30, 60, 120, 1440)

# How long after start a reminder keeps nagging before it gives up.
REMINDER_GRACE_MINUTES = 20

# Scheduling-assistant working day (local time).
WORKDAY_START = dtime(8, 0)
WORKDAY_END = dtime(18, 0)
SLOT_STEP_MINUTES = 30

# Hard ceiling on how much calendar a single feed request may span.
MAX_FEED_DAYS = 120

# 🔌 WebSocket transport.
#
# `ChatConsumer` already implements `chat_poll` (that's how live poll results
# reach the browser) and it just forwards `event['payload']` verbatim. We ride
# that same handler so this feature needs no consumer changes to work. The
# CLIENT dispatches on `payload['type']`, which is 'meeting_update' — so
# nothing gets confused with actual polls.
#
# If you'd rather have a dedicated handler, add this to ChatConsumer:
#
#     async def chat_meeting(self, event):
#         await self.send(text_data=json.dumps(event['payload']))
#
# …then change the constant below to 'chat.meeting'. Nothing else moves.
MEETING_WS_CHANNEL_TYPE = 'chat.poll'

RSVP_ACTIVE = ('invited', 'accepted', 'tentative')
RSVP_BUSY = ('accepted', 'tentative', 'attended')


# ─────────────────────────────────────────────────────────────────────────────
# Small helpers
# ─────────────────────────────────────────────────────────────────────────────

def _lead_key(meeting_id):
    return f'eo_cal_lead:{meeting_id}'


def _dismiss_key(user_id, meeting_id):
    return f'eo_cal_dismiss:{user_id}:{meeting_id}'


def _get_reminder_minutes(meeting):
    val = cache.get(_lead_key(meeting.id))
    try:
        val = int(val)
    except (TypeError, ValueError):
        return DEFAULT_REMINDER_MINUTES
    return val if val in REMINDER_CHOICES else DEFAULT_REMINDER_MINUTES


def _set_reminder_minutes(meeting, minutes):
    try:
        minutes = int(minutes)
    except (TypeError, ValueError):
        return
    if minutes not in REMINDER_CHOICES:
        return
    # Keep it around until well after the meeting has been and gone.
    ttl = max(int((meeting.end_datetime - timezone.now()).total_seconds()) + 86400, 3600)
    cache.set(_lead_key(meeting.id), minutes, ttl)


def _parse_dt(value, field='date/time'):
    """Accept the handful of shapes an <input type=datetime-local> can send."""
    if not value:
        raise ValueError(f'{field} is required.')
    raw = str(value).strip().replace('Z', '')
    for fmt in ('%Y-%m-%dT%H:%M', '%Y-%m-%d %H:%M',
                '%Y-%m-%dT%H:%M:%S', '%Y-%m-%d %H:%M:%S'):
        try:
            parsed = datetime.strptime(raw, fmt)
            break
        except ValueError:
            continue
    else:
        raise ValueError(f'Could not read the {field}. Expected e.g. 2026-08-14 09:30.')

    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, timezone.get_current_timezone())
    return parsed


def _parse_date(value):
    try:
        return datetime.strptime(str(value).strip()[:10], '%Y-%m-%d').date()
    except Exception:
        return None


def _duration_display(start, end):
    mins = max(int((end - start).total_seconds() // 60), 0)
    if mins < 60:
        return f'{mins} min'
    hours, rem = divmod(mins, 60)
    if not rem:
        return f'{hours} hr' if hours == 1 else f'{hours} hrs'
    return f'{hours}h {rem}m'


def _relative_day(dt):
    """'Today' / 'Tomorrow' / 'Tue, 18 Aug' — the label people actually scan for."""
    local = timezone.localtime(dt)
    today = timezone.localdate()
    delta = (local.date() - today).days
    if delta == 0:
        return 'Today'
    if delta == 1:
        return 'Tomorrow'
    if delta == -1:
        return 'Yesterday'
    if 0 < delta < 7:
        return local.strftime('%A')
    return local.strftime('%a, %d %b')


def _user_brief(user):
    avatar = ''
    try:
        if getattr(user, 'avatar', None) and user.avatar:
            avatar = user.avatar.url
    except Exception:
        avatar = ''
    return {
        'id': str(user.id),
        'name': _safe_full_name(user),
        'initials': _safe_initials(user),
        'avatar_url': avatar,
    }


def _room_meeting_ids(room):
    """
    Every meeting that was invited *from this room*.

    Derived from the invite messages themselves, which is why this feature
    needs no FK on Meeting. Falls back to a Python scan if the database
    can't do JSON key lookups.
    """
    qs = ChatMessage.objects.filter(room=room, message_type='command', is_deleted=False)
    try:
        ids = list(
            qs.filter(command_payload__command_type='meeting_invite')
              .values_list('command_payload__meeting_id', flat=True)
        )
    except Exception:
        ids = [
            (m.command_payload or {}).get('meeting_id')
            for m in qs.only('command_payload')
            if (m.command_payload or {}).get('command_type') == 'meeting_invite'
        ]
    return [i for i in ids if i]


def _visible_meetings_for(user):
    """Meetings on this user's own calendar: theirs to run, or theirs to attend."""
    return (
        Meeting.objects
        .filter(Q(organizer=user) | Q(attendees__user=user))
        .distinct()
    )


def _can_manage_meeting(user, meeting):
    return bool(
        user and getattr(user, 'is_authenticated', False) and (
            meeting.organizer_id == user.id or user.is_superuser
        )
    )


# ─────────────────────────────────────────────────────────────────────────────
# Serialisation
# ─────────────────────────────────────────────────────────────────────────────

def _serialize_meeting(meeting, viewer, room_id=None, with_attendees=True):
    """
    One meeting, shaped for the invite card, the calendar grid and the
    reminder banner. Everything the UI needs, nothing it doesn't.
    """
    now = timezone.now()
    start = meeting.start_datetime
    end = meeting.end_datetime

    counts = {'accepted': 0, 'tentative': 0, 'declined': 0, 'pending': 0, 'total': 0}
    attendees = []
    my_rsvp = ''
    am_invited = False

    for att in meeting.attendees.select_related('user').all():
        counts['total'] += 1
        bucket = {
            'accepted': 'accepted', 'attended': 'accepted',
            'tentative': 'tentative',
            'declined': 'declined', 'no_show': 'declined',
        }.get(att.rsvp, 'pending')
        counts[bucket] += 1

        if viewer is not None and att.user_id == getattr(viewer, 'id', None):
            my_rsvp = att.rsvp
            am_invited = True

        if with_attendees and len(attendees) < 40:
            brief = _user_brief(att.user)
            brief['rsvp'] = att.rsvp
            brief['bucket'] = bucket
            brief['is_required'] = att.is_required
            brief['role'] = att.role or ''
            attendees.append(brief)

    is_cancelled = meeting.status == 'cancelled'
    minutes_until = int((start - now).total_seconds() // 60)

    return {
        'id': str(meeting.id),
        'title': meeting.title,
        'description': meeting.description or '',
        'agenda': meeting.agenda or '',

        'organizer': _user_brief(meeting.organizer) if meeting.organizer_id else None,
        'organizer_id': str(meeting.organizer_id or ''),
        'is_organizer': bool(viewer is not None and meeting.organizer_id == getattr(viewer, 'id', None)),
        'can_manage': _can_manage_meeting(viewer, meeting),

        'start': timezone.localtime(start).isoformat(),
        'end': timezone.localtime(end).isoformat(),
        'day_label': _relative_day(start),
        'date_display': timezone.localtime(start).strftime('%a, %d %b %Y'),
        'time_display': '{}–{}'.format(
            timezone.localtime(start).strftime('%H:%M'),
            timezone.localtime(end).strftime('%H:%M'),
        ),
        'duration_display': _duration_display(start, end),
        'minutes_until': minutes_until,

        'location': meeting.location or '',
        'virtual_link': meeting.virtual_link or '',
        'meeting_type': meeting.meeting_type,
        'meeting_type_display': meeting.get_meeting_type_display(),
        'type_color': meeting.type_color,
        'type_icon': meeting.type_icon,

        'status': meeting.status,
        'status_display': meeting.get_status_display(),
        'is_cancelled': is_cancelled,
        'is_past': end < now,
        'is_live': (not is_cancelled) and start <= now <= end,

        'recurrence': meeting.recurrence,
        'recurrence_display': meeting.get_recurrence_display(),
        'is_recurring_instance': meeting.is_recurring_instance,

        'my_rsvp': my_rsvp,
        'am_invited': am_invited,
        'can_rsvp': bool(am_invited and not is_cancelled and end > now),

        'counts': counts,
        'attendees': attendees,

        'reminder_minutes': _get_reminder_minutes(meeting),
        'detail_url': f'/meetings/{meeting.id}/',
        'room_id': str(room_id) if room_id else '',
    }


def _meeting_queryset():
    return (
        Meeting.objects
        .select_related('organizer', 'project')
        .prefetch_related('attendees__user')
    )


# ─────────────────────────────────────────────────────────────────────────────
# Broadcasting
# ─────────────────────────────────────────────────────────────────────────────

def _broadcast_meeting_update(meeting, room_ids=None):
    """
    Push a fresh meeting payload to every room that carries an invite for it,
    so open cards re-render the moment somebody responds.

    Serialised with viewer=None: the payload holds the shared facts (tally,
    status, times). Each client re-applies its own `my_rsvp` locally, and the
    next hydrate/poll fetches the authoritative per-viewer version anyway.
    """
    try:
        from channels.layers import get_channel_layer
        from asgiref.sync import async_to_sync

        layer = get_channel_layer()
        if layer is None:
            return

        if room_ids is None:
            room_ids = _rooms_carrying_meeting(meeting)

        payload = {
            'type': 'meeting_update',
            'meeting_id': str(meeting.id),
            'meeting': _serialize_meeting(meeting, None),
        }

        for rid in set(str(r) for r in room_ids if r):
            async_to_sync(layer.group_send)(
                _room_group_name(rid),
                {'type': MEETING_WS_CHANNEL_TYPE, 'payload': dict(payload, room_id=rid)},
            )
    except Exception:
        log.exception('calendar: meeting broadcast failed')


def _rooms_carrying_meeting(meeting):
    qs = ChatMessage.objects.filter(message_type='command', is_deleted=False)
    try:
        return list(
            qs.filter(
                command_payload__command_type='meeting_invite',
                command_payload__meeting_id=str(meeting.id),
            ).values_list('room_id', flat=True)
        )
    except Exception:
        out = []
        for m in qs.only('room_id', 'command_payload'):
            p = m.command_payload or {}
            if p.get('command_type') == 'meeting_invite' and p.get('meeting_id') == str(meeting.id):
                out.append(m.room_id)
        return out


def _post_invite_message(room, meeting, sender):
    """Drop the interactive invite card into the room."""
    when = '{} · {}'.format(
        _relative_day(meeting.start_datetime),
        timezone.localtime(meeting.start_datetime).strftime('%H:%M'),
    )
    msg = ChatMessage.objects.create(
        room=room,
        sender=sender,
        message_type='command',
        content=f'📅 Meeting invite: {meeting.title} — {when}',
        command_payload={
            'command_type': 'meeting_invite',
            'meeting_id': str(meeting.id),
            'title': meeting.title,
            'start': timezone.localtime(meeting.start_datetime).isoformat(),
            'end': timezone.localtime(meeting.end_datetime).isoformat(),
            'location': meeting.location or '',
            'virtual_link': meeting.virtual_link or '',
            'organizer_name': _safe_full_name(sender),
        },
    )
    room.updated_at = timezone.now()
    room.save(update_fields=['updated_at'])
    _broadcast_chat_message(msg, viewer=sender)
    return msg


# ─────────────────────────────────────────────────────────────────────────────
# Create
# ─────────────────────────────────────────────────────────────────────────────

class ChatMeetingCreateView(LoginRequiredMixin, View):
    """
    POST /messages/<room_id>/calendar/invite/

    Fields
        title            required
        start            required   'YYYY-MM-DDTHH:MM'
        end              required   (or send duration_minutes instead)
        duration_minutes optional   used when `end` is absent
        all_day          '1' → 00:00 to 23:59 on the start date
        attendees[]      user ids; defaults to everyone in the room
        location, virtual_link, description, agenda
        meeting_type     one of Meeting.MeetingType
        recurrence       one of Meeting.Recurrence
        recurrence_count int, capped at 52
        reminder_minutes one of REMINDER_CHOICES
        online_meeting   '1' → auto-fill virtual_link with the room's call URL
    """

    def post(self, request, room_id):
        room = get_object_or_404(ChatRoom, id=room_id, members=request.user)

        if not _can_post_in_room(request.user, room):
            return JsonResponse(
                {'ok': False, 'error': 'This room is read-only, so invites can’t be sent here.'},
                status=403,
            )

        title = (request.POST.get('title') or '').strip()
        if not title:
            return JsonResponse({'ok': False, 'error': 'Give the meeting a title.'}, status=400)

        # ── When ──────────────────────────────────────────────────────────
        try:
            start = _parse_dt(request.POST.get('start'), 'start time')
        except ValueError as e:
            return JsonResponse({'ok': False, 'error': str(e)}, status=400)

        if request.POST.get('all_day') == '1':
            local_start = timezone.localtime(start)
            start = local_start.replace(hour=0, minute=0, second=0, microsecond=0)
            end = start + timedelta(hours=23, minutes=59)
        elif request.POST.get('end'):
            try:
                end = _parse_dt(request.POST.get('end'), 'end time')
            except ValueError as e:
                return JsonResponse({'ok': False, 'error': str(e)}, status=400)
        else:
            try:
                minutes = max(int(request.POST.get('duration_minutes') or 30), 5)
            except (TypeError, ValueError):
                minutes = 30
            end = start + timedelta(minutes=minutes)

        if end <= start:
            return JsonResponse(
                {'ok': False, 'error': 'The meeting ends before it starts. Check the times.'},
                status=400,
            )
        if (end - start) > timedelta(days=7):
            return JsonResponse({'ok': False, 'error': 'A single meeting can’t run longer than 7 days.'}, status=400)

        # ── Who ───────────────────────────────────────────────────────────
        requested = [i for i in request.POST.getlist('attendees[]') if i]
        member_ids = set(str(i) for i in room.members.values_list('id', flat=True))

        if requested:
            # Anyone can be invited, but people outside the room get told
            # about it in a DM rather than silently added to this room.
            invitees = list(User.objects.filter(id__in=requested, is_active=True))
        else:
            invitees = list(room.members.filter(is_active=True))

        invitees = [u for u in invitees if u.id != request.user.id]
        if not invitees:
            return JsonResponse({'ok': False, 'error': 'Pick at least one person to invite.'}, status=400)

        # ── Type / recurrence ─────────────────────────────────────────────
        meeting_type = (request.POST.get('meeting_type') or '').strip()
        if meeting_type not in dict(Meeting.MeetingType.choices):
            meeting_type = (
                Meeting.MeetingType.ONE_ON_ONE
                if room.room_type == 'direct' and len(invitees) == 1
                else Meeting.MeetingType.TEAM
            )

        recurrence = (request.POST.get('recurrence') or 'none').strip()
        if recurrence not in dict(Meeting.Recurrence.choices):
            recurrence = 'none'

        try:
            rec_count = min(max(int(request.POST.get('recurrence_count') or 0), 0), 52)
        except (TypeError, ValueError):
            rec_count = 0

        virtual_link = (request.POST.get('virtual_link') or '').strip()
        if not virtual_link and request.POST.get('online_meeting') == '1':
            virtual_link = request.build_absolute_uri(f'/messages/call/window/{room.id}/')

        # ── Create ────────────────────────────────────────────────────────
        meeting = Meeting.objects.create(
            title=title,
            description=(request.POST.get('description') or '').strip(),
            agenda=(request.POST.get('agenda') or '').strip(),
            meeting_type=meeting_type,
            organizer=request.user,
            start_datetime=start,
            end_datetime=end,
            location=(request.POST.get('location') or '').strip(),
            virtual_link=virtual_link,
            project=room.project if room.project_id else None,
            unit=room.unit if room.unit_id else None,
            department=room.department if room.department_id else None,
            recurrence=recurrence,
            recurrence_count=rec_count or None,
        )

        MeetingAttendee.objects.get_or_create(
            meeting=meeting,
            user=request.user,
            defaults={
                'rsvp': MeetingAttendee.RSVP.ACCEPTED,
                'role': 'Organiser',
                'is_required': True,
                'responded_at': timezone.now(),
            },
        )
        for u in invitees:
            MeetingAttendee.objects.get_or_create(
                meeting=meeting,
                user=u,
                defaults={'rsvp': MeetingAttendee.RSVP.INVITED, 'is_required': True},
            )

        _set_reminder_minutes(meeting, request.POST.get('reminder_minutes', DEFAULT_REMINDER_MINUTES))

        # Recurring series — reuse the meetings app's own generator so the
        # /meetings/ pages see exactly what they expect.
        if recurrence != 'none' and rec_count:
            try:
                from apps.meetings.views import _generate_recurrence_instances
                _generate_recurrence_instances(
                    meeting, [u.id for u in invitees], []
                )
            except Exception:
                log.exception('calendar: recurrence generation failed')

        # ── Announce ──────────────────────────────────────────────────────
        msg = _post_invite_message(room, meeting, request.user)

        ChatRoomMember.objects.filter(room=room, user=request.user).update(last_read=timezone.now())

        try:
            _notify_offline_members(room, request.user, msg)
        except Exception:
            pass

        # People invited who aren't in this room still deserve the card.
        outsiders = [u for u in invitees if str(u.id) not in member_ids]
        if outsiders:
            try:
                _fan_out_to_dms(request, meeting, outsiders)
            except Exception:
                log.exception('calendar: DM fan-out failed')

        try:
            from apps.core.models import CoreNotification
            for u in invitees:
                CoreNotification.objects.create(
                    recipient=u,
                    sender=request.user,
                    notification_type='meeting',
                    title=f'Meeting invite: {meeting.title}',
                    message='{} invited you — {} at {}.'.format(
                        _safe_full_name(request.user),
                        _relative_day(start),
                        timezone.localtime(start).strftime('%H:%M'),
                    ),
                    link=f'/meetings/{meeting.id}/',
                )
        except Exception:
            pass

        return JsonResponse({
            'ok': True,
            'meeting': _serialize_meeting(meeting, request.user, room_id=room.id),
            'payload': _serialize_chat_message(msg, viewer=request.user),
        })


def _fan_out_to_dms(request, meeting, users):
    """Post the same invite card into a 1-to-1 room with each outside invitee."""
    for u in users:
        room = (
            ChatRoom.objects
            .filter(room_type='direct', members=request.user)
            .filter(members=u)
            .first()
        )
        if room is None:
            room = ChatRoom.objects.create(room_type='direct', created_by=request.user)
            ChatRoomMember.objects.get_or_create(room=room, user=request.user)
            ChatRoomMember.objects.get_or_create(room=room, user=u)
        _post_invite_message(room, meeting, request.user)


# ─────────────────────────────────────────────────────────────────────────────
# Read
# ─────────────────────────────────────────────────────────────────────────────

class ChatMeetingStateView(LoginRequiredMixin, View):
    """
    POST /messages/calendar/meetings/state/     body: {"meeting_ids": [...]}

    Batch hydrate for invite cards. Mirrors PollStateView: the message stream
    renders empty mounts, this fills them with live RSVP state in one round
    trip. Also used after a reconnect to repair anything missed.
    """

    def post(self, request):
        try:
            body = json.loads(request.body.decode('utf-8') or '{}')
        except Exception:
            body = {}

        ids = [str(i) for i in (body.get('meeting_ids') or [])][:100]
        if not ids:
            return JsonResponse({'ok': True, 'meetings': []})

        meetings = _meeting_queryset().filter(id__in=ids)

        return JsonResponse({
            'ok': True,
            'meetings': [_serialize_meeting(m, request.user) for m in meetings],
        })


class ChatCalendarFeedView(LoginRequiredMixin, View):
    """
    GET /messages/calendar/feed/?start=YYYY-MM-DD&end=YYYY-MM-DD

    Optional
        room_id=<uuid>   only meetings invited from that room
        include_declined=1
    """

    def get(self, request):
        start_d = _parse_date(request.GET.get('start')) or timezone.localdate().replace(day=1)
        end_d = _parse_date(request.GET.get('end')) or (start_d + timedelta(days=42))

        if end_d < start_d:
            start_d, end_d = end_d, start_d
        if (end_d - start_d).days > MAX_FEED_DAYS:
            end_d = start_d + timedelta(days=MAX_FEED_DAYS)

        tz = timezone.get_current_timezone()
        start_dt = timezone.make_aware(datetime.combine(start_d, dtime.min), tz)
        end_dt = timezone.make_aware(datetime.combine(end_d, dtime.max), tz)

        qs = (
            _visible_meetings_for(request.user)
            .filter(start_datetime__lte=end_dt, end_datetime__gte=start_dt)
            .select_related('organizer')
            .prefetch_related('attendees__user')
            .order_by('start_datetime')
        )

        room_id = (request.GET.get('room_id') or '').strip()
        if room_id:
            room = get_object_or_404(ChatRoom, id=room_id, members=request.user)
            qs = qs.filter(id__in=_room_meeting_ids(room))

        if request.GET.get('include_declined') != '1':
            qs = qs.exclude(attendees__user=request.user, attendees__rsvp='declined')

        events = [
            _serialize_meeting(m, request.user, with_attendees=False)
            for m in qs[:400]
        ]

        return JsonResponse({
            'ok': True,
            'start': start_d.isoformat(),
            'end': end_d.isoformat(),
            'today': timezone.localdate().isoformat(),
            'events': events,
        })


class ChatMeetingDetailView(LoginRequiredMixin, View):
    """GET /messages/calendar/meeting/<meeting_id>/ — one meeting, full detail."""

    def get(self, request, meeting_id):
        meeting = get_object_or_404(_meeting_queryset(), id=meeting_id)

        is_attendee = meeting.attendees.filter(user=request.user).exists()
        if not (is_attendee or meeting.organizer_id == request.user.id or request.user.is_superuser):
            return JsonResponse({'ok': False, 'error': 'You’re not on this meeting.'}, status=403)

        return JsonResponse({'ok': True, 'meeting': _serialize_meeting(meeting, request.user)})


# ─────────────────────────────────────────────────────────────────────────────
# Respond
# ─────────────────────────────────────────────────────────────────────────────

class ChatMeetingRSVPView(LoginRequiredMixin, View):
    """
    POST /messages/calendar/meeting/<meeting_id>/rsvp/   body: rsvp=accepted|tentative|declined

    Accepting is what puts the meeting on someone's calendar — the feed reads
    straight off these rows, so there's no second "add to calendar" step and
    nothing that can drift out of sync.
    """

    ALLOWED = ('accepted', 'tentative', 'declined')

    def post(self, request, meeting_id):
        meeting = get_object_or_404(_meeting_queryset(), id=meeting_id)

        choice = (request.POST.get('rsvp') or '').strip().lower()
        if choice not in self.ALLOWED:
            return JsonResponse({'ok': False, 'error': 'Choose accept, tentative, or decline.'}, status=400)

        try:
            attendee = meeting.attendees.get(user=request.user)
        except MeetingAttendee.DoesNotExist:
            return JsonResponse({'ok': False, 'error': 'You weren’t invited to this meeting.'}, status=403)

        if meeting.status == 'cancelled':
            return JsonResponse({'ok': False, 'error': 'This meeting was cancelled.'}, status=400)
        if meeting.end_datetime < timezone.now():
            return JsonResponse({'ok': False, 'error': 'This meeting has already finished.'}, status=400)

        previous = attendee.rsvp
        attendee.rsvp = choice
        attendee.responded_at = timezone.now()
        attendee.notes = (request.POST.get('note') or '').strip()[:500] or attendee.notes
        attendee.save(update_fields=['rsvp', 'responded_at', 'notes'])

        # Declining takes it off your calendar, so the reminder goes too.
        if choice == 'declined':
            cache.set(_dismiss_key(request.user.id, meeting.id), 'dismissed', 86400 * 30)
        else:
            cache.delete(_dismiss_key(request.user.id, meeting.id))

        if previous != choice:
            self._tell_the_organiser(request, meeting, choice)

        meeting.refresh_from_db()
        _broadcast_meeting_update(meeting)

        return JsonResponse({
            'ok': True,
            'meeting': _serialize_meeting(meeting, request.user),
        })

    def _tell_the_organiser(self, request, meeting, choice):
        if not meeting.organizer_id or meeting.organizer_id == request.user.id:
            return

        verb = {'accepted': 'accepted', 'tentative': 'tentatively accepted', 'declined': 'declined'}[choice]
        try:
            from apps.core.models import CoreNotification
            CoreNotification.objects.create(
                recipient=meeting.organizer,
                sender=request.user,
                notification_type='meeting',
                title=f'{_safe_full_name(request.user)} {verb} “{meeting.title}”',
                message='{} {} your invite for {} at {}.'.format(
                    _safe_full_name(request.user), verb,
                    _relative_day(meeting.start_datetime),
                    timezone.localtime(meeting.start_datetime).strftime('%H:%M'),
                ),
                link=f'/meetings/{meeting.id}/',
            )
        except Exception:
            pass


class ChatMeetingCancelView(LoginRequiredMixin, View):
    """POST /messages/calendar/meeting/<meeting_id>/cancel/ — organiser only."""

    def post(self, request, meeting_id):
        meeting = get_object_or_404(_meeting_queryset(), id=meeting_id)

        if not _can_manage_meeting(request.user, meeting):
            return JsonResponse({'ok': False, 'error': 'Only the organiser can cancel this meeting.'}, status=403)
        if meeting.status == 'cancelled':
            return JsonResponse({'ok': True, 'meeting': _serialize_meeting(meeting, request.user)})

        meeting.status = 'cancelled'
        meeting.save(update_fields=['status'])

        reason = (request.POST.get('reason') or '').strip()
        note = f' — {reason}' if reason else ''
        room_ids = _rooms_carrying_meeting(meeting)

        for rid in set(room_ids):
            try:
                room = ChatRoom.objects.get(id=rid)
            except ChatRoom.DoesNotExist:
                continue
            msg = ChatMessage.objects.create(
                room=room,
                sender=request.user,
                message_type='system',
                content='📅 {} cancelled “{}” ({} at {}){}'.format(
                    _safe_full_name(request.user), meeting.title,
                    _relative_day(meeting.start_datetime),
                    timezone.localtime(meeting.start_datetime).strftime('%H:%M'),
                    note,
                ),
            )
            _broadcast_chat_message(msg, viewer=request.user)

        for att in meeting.attendees.exclude(user=request.user).select_related('user'):
            cache.set(_dismiss_key(att.user_id, meeting.id), 'dismissed', 86400 * 30)
            try:
                from apps.core.models import CoreNotification
                CoreNotification.objects.create(
                    recipient=att.user,
                    sender=request.user,
                    notification_type='meeting',
                    title=f'Cancelled: {meeting.title}',
                    message=f'{_safe_full_name(request.user)} cancelled this meeting{note}.',
                    link=f'/meetings/{meeting.id}/',
                )
            except Exception:
                pass

        _broadcast_meeting_update(meeting, room_ids=room_ids)

        return JsonResponse({'ok': True, 'meeting': _serialize_meeting(meeting, request.user)})


# ─────────────────────────────────────────────────────────────────────────────
# Reminders
# ─────────────────────────────────────────────────────────────────────────────

class ChatMeetingRemindersView(LoginRequiredMixin, View):
    """
    GET /messages/calendar/reminders/

    Returns meetings that are inside their lead time and haven't been
    dismissed or snoozed. The client polls this every 30s alongside the
    notification poll it already runs — no Celery beat, no cron.

    A reminder keeps coming back until the person dismisses or snoozes it,
    which is how Outlook behaves and is the whole point: a reminder you can
    miss by looking away isn't a reminder.
    """

    def get(self, request):
        now = timezone.now()
        horizon = now + timedelta(minutes=max(REMINDER_CHOICES))

        candidates = (
            Meeting.objects
            .filter(
                attendees__user=request.user,
                attendees__rsvp__in=RSVP_ACTIVE,
                status__in=('scheduled', 'in_progress'),
                start_datetime__lte=horizon,
                start_datetime__gte=now - timedelta(minutes=REMINDER_GRACE_MINUTES),
            )
            .select_related('organizer')
            .prefetch_related('attendees__user')
            .distinct()
            .order_by('start_datetime')[:20]
        )

        due = []
        for m in candidates:
            lead = _get_reminder_minutes(m)
            minutes_until = (m.start_datetime - now).total_seconds() / 60.0
            if minutes_until > lead:
                continue

            snooze = cache.get(_dismiss_key(request.user.id, m.id))
            if snooze == 'dismissed':
                continue
            if snooze:
                try:
                    if now < datetime.fromisoformat(snooze):
                        continue
                except Exception:
                    pass

            data = _serialize_meeting(m, request.user, with_attendees=False)
            data['minutes_until'] = int(round(minutes_until))
            data['is_starting_now'] = -1 <= minutes_until <= 1
            data['has_started'] = minutes_until < -1
            due.append(data)

        return JsonResponse({'ok': True, 'now': now.isoformat(), 'reminders': due})


class ChatMeetingReminderDismissView(LoginRequiredMixin, View):
    """
    POST /messages/calendar/meeting/<meeting_id>/reminder/
        action=dismiss              stop reminding about this one
        action=snooze&minutes=5     come back in N minutes
    """

    def post(self, request, meeting_id):
        meeting = get_object_or_404(Meeting, id=meeting_id)

        if not meeting.attendees.filter(user=request.user).exists():
            return JsonResponse({'ok': False, 'error': 'You’re not on this meeting.'}, status=403)

        action = (request.POST.get('action') or 'dismiss').strip()
        ttl = max(int((meeting.end_datetime - timezone.now()).total_seconds()) + 3600, 3600)

        if action == 'snooze':
            try:
                minutes = min(max(int(request.POST.get('minutes') or 5), 1), 1440)
            except (TypeError, ValueError):
                minutes = 5
            until = timezone.now() + timedelta(minutes=minutes)
            cache.set(_dismiss_key(request.user.id, meeting.id), until.isoformat(), ttl)
            return JsonResponse({'ok': True, 'snoozed_until': until.isoformat()})

        cache.set(_dismiss_key(request.user.id, meeting.id), 'dismissed', ttl)
        return JsonResponse({'ok': True, 'dismissed': True})


# ─────────────────────────────────────────────────────────────────────────────
# Scheduling assistant
# ─────────────────────────────────────────────────────────────────────────────

class ChatMeetingAvailabilityView(LoginRequiredMixin, View):
    """
    POST /messages/calendar/availability/

    Body
        user_ids[]        who to check
        date              YYYY-MM-DD
        duration_minutes  how long the meeting needs to be
        exclude_meeting   optional, ignore this meeting when rescheduling

    Returns each person's busy blocks for that day plus the first few slots
    where everybody is free. Titles are withheld from meetings the viewer
    isn't on — you get to see *that* someone is busy, never with whom.
    """

    def post(self, request):
        user_ids = [i for i in request.POST.getlist('user_ids[]') if i]
        day = _parse_date(request.POST.get('date')) or timezone.localdate()

        try:
            duration = min(max(int(request.POST.get('duration_minutes') or 30), 5), 480)
        except (TypeError, ValueError):
            duration = 30

        exclude_id = (request.POST.get('exclude_meeting') or '').strip()

        tz = timezone.get_current_timezone()
        day_start = timezone.make_aware(datetime.combine(day, dtime.min), tz)
        day_end = timezone.make_aware(datetime.combine(day, dtime.max), tz)

        users = list(User.objects.filter(id__in=user_ids, is_active=True))
        if request.user.id not in [u.id for u in users]:
            users.append(request.user)

        qs = (
            Meeting.objects
            .filter(
                attendees__user__in=users,
                attendees__rsvp__in=RSVP_BUSY,
                start_datetime__lte=day_end,
                end_datetime__gte=day_start,
            )
            .exclude(status='cancelled')
            .prefetch_related('attendees')
            .distinct()
        )
        if exclude_id:
            qs = qs.exclude(id=exclude_id)

        meetings = list(qs)
        my_meeting_ids = {
            str(m.id) for m in meetings
            if m.organizer_id == request.user.id
            or any(a.user_id == request.user.id for a in m.attendees.all())
        }

        busy_by_user = {str(u.id): [] for u in users}
        all_blocks = []

        for m in meetings:
            block_start = max(m.start_datetime, day_start)
            block_end = min(m.end_datetime, day_end)
            visible_title = m.title if str(m.id) in my_meeting_ids else 'Busy'

            for att in m.attendees.all():
                key = str(att.user_id)
                if key not in busy_by_user or att.rsvp not in RSVP_BUSY:
                    continue
                busy_by_user[key].append({
                    'start': timezone.localtime(block_start).isoformat(),
                    'end': timezone.localtime(block_end).isoformat(),
                    'title': visible_title,
                    'start_minutes': self._minutes_into_day(block_start),
                    'end_minutes': self._minutes_into_day(block_end),
                })
                all_blocks.append((block_start, block_end))

        return JsonResponse({
            'ok': True,
            'date': day.isoformat(),
            'workday_start_minutes': WORKDAY_START.hour * 60 + WORKDAY_START.minute,
            'workday_end_minutes': WORKDAY_END.hour * 60 + WORKDAY_END.minute,
            'people': [
                dict(_user_brief(u), busy=busy_by_user.get(str(u.id), []))
                for u in users
            ],
            'suggestions': self._suggest(day, duration, all_blocks),
        })

    @staticmethod
    def _minutes_into_day(dt):
        local = timezone.localtime(dt)
        return local.hour * 60 + local.minute

    @staticmethod
    def _suggest(day, duration, blocks, limit=4):
        """First few workday slots of `duration` where nothing overlaps."""
        tz = timezone.get_current_timezone()
        cursor = timezone.make_aware(datetime.combine(day, WORKDAY_START), tz)
        close = timezone.make_aware(datetime.combine(day, WORKDAY_END), tz)
        now = timezone.now()

        out = []
        while cursor + timedelta(minutes=duration) <= close and len(out) < limit:
            slot_end = cursor + timedelta(minutes=duration)
            if cursor >= now and not any(cursor < b_end and slot_end > b_start
                                         for b_start, b_end in blocks):
                out.append({
                    'start': timezone.localtime(cursor).isoformat(),
                    'end': timezone.localtime(slot_end).isoformat(),
                    'label': '{}–{}'.format(
                        timezone.localtime(cursor).strftime('%H:%M'),
                        timezone.localtime(slot_end).strftime('%H:%M'),
                    ),
                })
            cursor += timedelta(minutes=SLOT_STEP_MINUTES)
        return out
