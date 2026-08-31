"""
apps/messaging/reminders.py
───────────────────────────
Reminders: create them, fire them on time, and let each person snooze or
resolve their own copy.

HOW A REMINDER ACTUALLY ARRIVES
───────────────────────────────
Firing is a Celery beat task (``sweep_due_reminders``) that runs every
minute and does two things: turns PENDING reminders whose time has come
into per-user receipts, and re-activates SNOOZED receipts whose snooze
has expired.

Delivery to the browser is deliberately BELT AND BRACES:

  1. A channel-layer push to the user's personal group (``user_<id>``),
     which ChatConsumer now joins alongside the room group. Instant, but
     only lands if that person has a chat socket open.
  2. A poll of ``/messages/reminders/open/``, which returns everything
     currently wanting their attention.

Only (2) is load-bearing. A reminder that fires while the user is on a
different page, or has no socket, or was asleep with the laptop shut,
still shows up the moment they next poll — including reminders that fired
hours ago, because "open" is a state in the database and not an event
that can be missed. The socket push exists so that a reminder set for
five minutes from now appears without waiting for the next poll tick.

That is also why the sweep is idempotent: it only ever moves receipts
forward through states, so a duplicated beat tick, a retried task or two
workers racing produce the same result as one clean run.

CELERY WIRING
─────────────
In the project's celery.py (or settings)::

    from celery.schedules import crontab

    app.conf.beat_schedule = {
        'messaging-sweep-reminders': {
            'task': 'apps.messaging.reminders.sweep_due_reminders',
            'schedule': 60.0,          # every minute
        },
    }

A minute of granularity is deliberate. Reminders are set to the minute
in the UI, nobody notices a 30-second lag, and a per-second beat would
run 86,400 no-op queries a day.

If Celery is ever unavailable the module still imports and every view
still works; reminders simply fire the next time the sweep runs, and
``sweep_due_reminders()`` can be called by hand or from cron::

    python manage.py shell -c "from apps.messaging.reminders import sweep_due_reminders; sweep_due_reminders()"

SETTINGS
────────
    MESSAGING_REMINDER_MAX_SNOOZE = 20      # per receipt, then it stops
    MESSAGING_REMINDER_EMAIL      = True    # allow the "email me too" box
"""

import logging
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db import transaction
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.utils.html import escape
from django.views.generic import View

from apps.messaging.models import (
    ChatMessage, ChatReminder, ChatReminderReceipt, ChatRoom,
)

log = logging.getLogger(__name__)

# Snooze presets the UI offers. Minutes, except 'tomorrow'.
SNOOZE_CHOICES = {
    '5':        5,
    '15':       15,
    '30':       30,
    '60':       60,
    '180':      180,
    'tomorrow': None,        # handled specially — 09:00 local tomorrow
}

DEFAULT_MAX_SNOOZE = 20


def _max_snooze():
    try:
        return int(getattr(settings, 'MESSAGING_REMINDER_MAX_SNOOZE',
                           DEFAULT_MAX_SNOOZE))
    except (TypeError, ValueError):
        return DEFAULT_MAX_SNOOZE


# ─────────────────────────────────────────────────────────────────────────────
# Creation
# ─────────────────────────────────────────────────────────────────────────────

class ReminderError(ValueError):
    """Something the user needs to fix."""


def _parse_when(value):
    """
    Accept an ISO datetime from the browser and return an aware datetime.

    A naive value is interpreted in the ACTIVE timezone, not UTC. Getting
    this backwards is the classic reminder bug: everything fires correctly
    in development (where the two coincide) and an hour or four out in
    production.
    """
    if not value:
        raise ReminderError('Pick a date and time.')
    when = parse_datetime(str(value))
    if when is None:
        raise ReminderError('That date and time could not be read.')
    if timezone.is_naive(when):
        when = timezone.make_aware(when, timezone.get_current_timezone())
    return when


def create_reminder(owner, *, title, remind_at, note='', room=None,
                    message=None, meeting_id=None, audience='me',
                    source='manual', notify_email=False):
    """Create a reminder. Returns the ChatReminder."""
    title = ' '.join((title or '').split())
    if not title:
        raise ReminderError('A reminder needs a title.')
    title = title[:200]

    when = _parse_when(remind_at)
    if when < timezone.now() - timedelta(minutes=1):
        raise ReminderError('That time has already passed.')

    if audience not in dict(ChatReminder.Audience.choices):
        audience = ChatReminder.Audience.ME
    # "Everyone invited" needs somebody to invite. Without a room there is
    # no attendee list, so quietly fall back rather than create a reminder
    # that can never reach anyone.
    if audience == ChatReminder.Audience.ATTENDEES and room is None:
        audience = ChatReminder.Audience.ME

    return ChatReminder.objects.create(
        owner=owner,
        title=title,
        note=(note or '')[:2000],
        room=room,
        message=message,
        meeting_id=meeting_id or None,
        remind_at=when,
        audience=audience,
        source=source if source in dict(ChatReminder.Source.choices) else 'manual',
        notify_email=bool(notify_email) and bool(
            getattr(settings, 'MESSAGING_REMINDER_EMAIL', True)),
    )


def recipients_for(reminder):
    """Who gets a receipt. Always includes the owner."""
    people = {reminder.owner_id: reminder.owner}
    if reminder.audience == ChatReminder.Audience.ATTENDEES and reminder.room_id:
        try:
            for user in reminder.room.members.all():
                people.setdefault(user.id, user)
        except Exception:
            log.exception('reminders: could not read room members for %s',
                          reminder.pk)
    return list(people.values())


# ─────────────────────────────────────────────────────────────────────────────
# Firing
# ─────────────────────────────────────────────────────────────────────────────

def fire(reminder):
    """
    Deliver one reminder: create/refresh a receipt per recipient, mark the
    reminder fired, and push to anyone with a socket open.

    Idempotent — running it twice produces the same receipts, and a
    receipt a user has already resolved is left alone rather than being
    resurrected.
    """
    now = timezone.now()
    delivered = []

    with transaction.atomic():
        row = (ChatReminder.objects
               .select_for_update()
               .filter(pk=reminder.pk)
               .first())
        if row is None or row.status == ChatReminder.Status.CANCELLED:
            return []
        # Another worker got here first.
        if row.status == ChatReminder.Status.FIRED:
            return []

        for user in recipients_for(row):
            receipt, created = ChatReminderReceipt.objects.get_or_create(
                reminder=row, user=user,
                defaults={'state': ChatReminderReceipt.State.ACTIVE,
                          'delivered_at': now},
            )
            if not created and receipt.state == ChatReminderReceipt.State.PENDING:
                receipt.state = ChatReminderReceipt.State.ACTIVE
                receipt.delivered_at = now
                receipt.save(update_fields=['state', 'delivered_at'])
            if receipt.state == ChatReminderReceipt.State.ACTIVE:
                delivered.append(receipt)

        row.status = ChatReminder.Status.FIRED
        row.fired_at = now
        row.save(update_fields=['status', 'fired_at'])

    for receipt in delivered:
        push_to_user(receipt)
        if reminder.notify_email:
            _email_receipt(receipt)

    return delivered


def wake_snoozed(receipt):
    """A snooze has expired — put it back on screen."""
    now = timezone.now()
    updated = (ChatReminderReceipt.objects
               .filter(pk=receipt.pk,
                       state=ChatReminderReceipt.State.SNOOZED)
               .update(state=ChatReminderReceipt.State.ACTIVE,
                       snoozed_until=None, delivered_at=now))
    if updated:
        receipt.refresh_from_db()
        push_to_user(receipt)
    return bool(updated)


def sweep_due_reminders():
    """
    The beat task. Returns a small dict so a manual run says what it did.

    Deliberately does no bulk-update cleverness: reminder volumes are
    tiny (a handful per person per day) and a readable loop that can be
    stepped through in a shell beats a query that has to be reverse
    engineered when a reminder doesn't arrive.
    """
    now = timezone.now()
    fired = snoozed = 0

    due = (ChatReminder.objects
           .filter(status=ChatReminder.Status.PENDING, remind_at__lte=now)
           .select_related('owner', 'room')[:500])
    for reminder in due:
        try:
            fired += len(fire(reminder))
        except Exception:
            log.exception('reminders: firing %s failed', reminder.pk)

    waking = (ChatReminderReceipt.objects
              .filter(state=ChatReminderReceipt.State.SNOOZED,
                      snoozed_until__lte=now)
              .select_related('reminder', 'user')[:500])
    for receipt in waking:
        try:
            if wake_snoozed(receipt):
                snoozed += 1
        except Exception:
            log.exception('reminders: waking %s failed', receipt.pk)

    if fired or snoozed:
        log.info('reminders: delivered %s, re-woke %s', fired, snoozed)
    return {'delivered': fired, 'rewoken': snoozed}


# Celery registration, if Celery is installed. The plain function above is
# what actually runs, so nothing here changes behaviour — it only gives
# beat a task name to call.
try:                                             # pragma: no cover
    from celery import shared_task

    sweep_due_reminders = shared_task(
        name='apps.messaging.reminders.sweep_due_reminders',
        ignore_result=True,
    )(sweep_due_reminders)
except Exception:                                # pragma: no cover
    log.info('reminders: Celery not available — sweep must be run by cron')


# ─────────────────────────────────────────────────────────────────────────────
# Delivery
# ─────────────────────────────────────────────────────────────────────────────

def user_group_name(user_id):
    return f'user_{user_id}'


def serialize(receipt, viewer=None):
    reminder = receipt.reminder
    return {
        'receipt_id':  str(receipt.pk),
        'reminder_id': str(reminder.pk),
        'title':       reminder.title,
        'note':        reminder.note,
        'remind_at':   timezone.localtime(reminder.remind_at).isoformat(),
        'fired_at':    (timezone.localtime(reminder.fired_at).isoformat()
                        if reminder.fired_at else None),
        'state':       receipt.state,
        'snoozed_until': (timezone.localtime(receipt.snoozed_until).isoformat()
                          if receipt.snoozed_until else None),
        'snooze_count': receipt.snooze_count,
        'audience':    reminder.audience,
        'source':      reminder.source,
        'room_id':     str(reminder.room_id) if reminder.room_id else '',
        'room_name':   (reminder.room.name if reminder.room_id and reminder.room
                        else ''),
        'message_id':  str(reminder.message_id) if reminder.message_id else '',
        'meeting_id':  str(reminder.meeting_id) if reminder.meeting_id else '',
        'is_owner':    bool(viewer and reminder.owner_id == viewer.id),
        'owner_name':  _name(reminder.owner),
        'url':         (f'/messages/{reminder.room_id}/'
                        if reminder.room_id else ''),
    }


def push_to_user(receipt):
    """Fire-and-forget socket push. The poll endpoint is the real guarantee."""
    try:
        from asgiref.sync import async_to_sync
        from channels.layers import get_channel_layer

        layer = get_channel_layer()
        if layer is None:
            return
        async_to_sync(layer.group_send)(
            user_group_name(receipt.user_id),
            {
                'type': 'reminder.event',
                'payload': {
                    'type': 'reminder_due',
                    'reminder': serialize(receipt, viewer=receipt.user),
                },
            },
        )
    except Exception:
        log.exception('reminders: push failed for %s', receipt.pk)


def _name(user):
    try:
        from apps.messaging.views import _safe_full_name
        return _safe_full_name(user)
    except Exception:
        return str(user)


def _email_receipt(receipt):
    """Optional email copy, sent in a thread so the sweep isn't held up."""
    import threading

    user = receipt.user
    if not getattr(user, 'email', ''):
        return
    reminder = receipt.reminder
    org_name = getattr(settings, 'ORGANISATION_NAME',
                       getattr(settings, 'OFFICE_NAME', 'EasyOffice'))
    from_email = getattr(settings, 'DEFAULT_FROM_EMAIL',
                         f'noreply@{org_name.lower().replace(" ", "")}.org')
    url = (getattr(settings, 'SITE_URL', '') or '').rstrip('/') + \
        (f'/messages/{reminder.room_id}/' if reminder.room_id else '/messages/')

    html = f"""<!DOCTYPE html><html><head><meta charset="UTF-8"/></head>
<body style="margin:0;background:#f1f5f9;font-family:'Segoe UI',Arial,sans-serif">
<div style="max-width:560px;margin:28px auto;background:#fff;border-radius:14px;overflow:hidden;box-shadow:0 4px 24px rgba(0,0,0,.08)">
  <div style="background:linear-gradient(135deg,#1e3a5f,#6366f1);padding:24px 32px">
    <div style="font-size:11px;letter-spacing:.12em;text-transform:uppercase;color:rgba(255,255,255,.75);font-weight:700">Reminder</div>
    <h1 style="margin:6px 0 0;font-size:18px;color:#fff;font-weight:700">{escape(reminder.title)}</h1>
  </div>
  <div style="padding:24px 32px">
    {'<p style="margin:0 0 18px;font-size:14px;color:#334155;line-height:1.6">' + escape(reminder.note).replace(chr(10), '<br>') + '</p>' if reminder.note else ''}
    <div style="text-align:center">
      <a href="{escape(url)}" style="display:inline-block;background:linear-gradient(135deg,#6366f1,#4f46e5);color:#fff;padding:12px 30px;border-radius:10px;text-decoration:none;font-weight:700;font-size:14px">Open {escape(org_name)}</a>
    </div>
  </div>
</div></body></html>"""

    def _send():
        from django.core.mail import EmailMessage
        try:
            mail = EmailMessage(subject=f'Reminder: {reminder.title}',
                                body=html, from_email=from_email,
                                to=[user.email])
            mail.content_subtype = 'html'
            mail.send()
        except Exception:
            log.exception('reminders: email to %s failed', user.pk)

    threading.Thread(target=_send, daemon=True).start()


# ─────────────────────────────────────────────────────────────────────────────
# Per-recipient actions
# ─────────────────────────────────────────────────────────────────────────────

def snooze(receipt, choice='15'):
    """Push one person's copy back. Returns the new wake time."""
    if receipt.snooze_count >= _max_snooze():
        raise ReminderError(
            'This reminder has been snoozed too many times — '
            'resolve it or change the time instead.')

    now = timezone.localtime(timezone.now())
    key = str(choice)
    if key == 'tomorrow':
        wake = (now + timedelta(days=1)).replace(
            hour=9, minute=0, second=0, microsecond=0)
    else:
        minutes = SNOOZE_CHOICES.get(key)
        if minutes is None:
            minutes = 15
        wake = now + timedelta(minutes=minutes)

    receipt.state = ChatReminderReceipt.State.SNOOZED
    receipt.snoozed_until = wake
    receipt.snooze_count += 1
    receipt.save(update_fields=['state', 'snoozed_until', 'snooze_count'])
    return wake


def resolve(receipt):
    receipt.state = ChatReminderReceipt.State.RESOLVED
    receipt.resolved_at = timezone.now()
    receipt.snoozed_until = None
    receipt.save(update_fields=['state', 'resolved_at', 'snoozed_until'])
    return receipt


def dismiss(receipt):
    """Close it without claiming the underlying thing got done."""
    receipt.state = ChatReminderReceipt.State.DISMISSED
    receipt.resolved_at = timezone.now()
    receipt.snoozed_until = None
    receipt.save(update_fields=['state', 'resolved_at', 'snoozed_until'])
    return receipt


def open_for(user):
    """Everything currently wanting this person's attention."""
    return (ChatReminderReceipt.objects
            .filter(user=user, state=ChatReminderReceipt.State.ACTIVE)
            .select_related('reminder', 'reminder__room', 'reminder__owner')
            .order_by('reminder__remind_at'))


# ─────────────────────────────────────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────────────────────────────────────

def _receipt_or_404(request, receipt_id):
    return get_object_or_404(
        ChatReminderReceipt.objects.select_related(
            'reminder', 'reminder__room', 'reminder__owner'),
        id=receipt_id, user=request.user)


class ReminderCreateView(LoginRequiredMixin, View):
    """
    POST /messages/reminders/create/

        title      required
        remind_at  required, ISO datetime ("2026-09-05T09:00")
        note       optional
        room_id    optional — required for audience=attendees
        message_id optional — the memo/invite it came from
        meeting_id optional
        audience   me | attendees
        email      '1' to also send an email when it fires
    """

    def post(self, request):
        room = None
        room_id = (request.POST.get('room_id') or '').strip()
        if room_id:
            room = ChatRoom.objects.filter(
                id=room_id, members=request.user).first()
            if room is None:
                return JsonResponse(
                    {'ok': False, 'error': 'You are not in that room.'},
                    status=403)

        message = None
        message_id = (request.POST.get('message_id') or '').strip()
        if message_id and room is not None:
            message = ChatMessage.objects.filter(
                id=message_id, room=room, is_deleted=False).first()

        try:
            reminder = create_reminder(
                request.user,
                title=request.POST.get('title', ''),
                remind_at=request.POST.get('remind_at', ''),
                note=request.POST.get('note', ''),
                room=room,
                message=message,
                meeting_id=(request.POST.get('meeting_id') or '').strip() or None,
                audience=request.POST.get('audience', 'me'),
                source=request.POST.get('source', 'manual'),
                notify_email=request.POST.get('email') == '1',
            )
        except ReminderError as exc:
            return JsonResponse({'ok': False, 'error': str(exc)}, status=400)
        except Exception:
            log.exception('reminders: create failed')
            return JsonResponse(
                {'ok': False, 'error': 'The reminder could not be saved.'},
                status=400)

        # Set for a moment that has already arrived (or arrives inside the
        # next sweep window) — deliver now rather than making the user
        # wonder whether it worked.
        if reminder.remind_at <= timezone.now() + timedelta(seconds=30):
            fire(reminder)

        return JsonResponse({
            'ok': True,
            'reminder': {
                'id': str(reminder.pk),
                'title': reminder.title,
                'remind_at': timezone.localtime(reminder.remind_at).isoformat(),
                'audience': reminder.audience,
                'status': reminder.status,
            },
        })


class ReminderOpenListView(LoginRequiredMixin, View):
    """
    GET /messages/reminders/open/

    The poller. Everything active for this user, oldest first. This is the
    endpoint that makes a reminder impossible to miss: state lives in the
    database, so a reminder that fired while the browser was closed is
    still here on the next request.
    """

    def get(self, request):
        rows = open_for(request.user)
        return JsonResponse({
            'ok': True,
            'now': timezone.localtime(timezone.now()).isoformat(),
            'reminders': [serialize(r, viewer=request.user) for r in rows],
        })


class ReminderUpcomingView(LoginRequiredMixin, View):
    """
    GET /messages/reminders/upcoming/?room_id=…

    Everything scheduled but not yet fired, for the reminders list. Scoped
    to one room when asked, so the chat page can show just its own.
    """

    def get(self, request):
        qs = (ChatReminder.objects
              .filter(Q(owner=request.user) | Q(receipts__user=request.user),
                      status=ChatReminder.Status.PENDING)
              .select_related('room', 'owner')
              .distinct()
              .order_by('remind_at')[:100])

        room_id = (request.GET.get('room_id') or '').strip()
        if room_id:
            qs = [r for r in qs if str(r.room_id) == room_id]

        return JsonResponse({
            'ok': True,
            'reminders': [{
                'id':        str(r.pk),
                'title':     r.title,
                'note':      r.note,
                'remind_at': timezone.localtime(r.remind_at).isoformat(),
                'audience':  r.audience,
                'source':    r.source,
                'room_id':   str(r.room_id) if r.room_id else '',
                'is_owner':  r.owner_id == request.user.id,
            } for r in qs],
        })


class ReminderSnoozeView(LoginRequiredMixin, View):
    """POST /messages/reminders/<receipt_id>/snooze/   body: choice=15"""

    def post(self, request, receipt_id):
        receipt = _receipt_or_404(request, receipt_id)
        try:
            wake = snooze(receipt, request.POST.get('choice', '15'))
        except ReminderError as exc:
            return JsonResponse({'ok': False, 'error': str(exc)}, status=400)
        return JsonResponse({
            'ok': True,
            'snoozed_until': timezone.localtime(wake).isoformat(),
            'reminder': serialize(receipt, viewer=request.user),
        })


class ReminderResolveView(LoginRequiredMixin, View):
    """POST /messages/reminders/<receipt_id>/resolve/  body: dismiss=1 to skip 'done'"""

    def post(self, request, receipt_id):
        receipt = _receipt_or_404(request, receipt_id)
        if request.POST.get('dismiss') == '1':
            dismiss(receipt)
        else:
            resolve(receipt)
        return JsonResponse({
            'ok': True,
            'reminder': serialize(receipt, viewer=request.user),
        })


class ReminderCalendarFeedView(LoginRequiredMixin, View):
    """
    GET /messages/reminders/feed/?start=YYYY-MM-DD&end=YYYY-MM-DD

    Reminders shaped as calendar events, in the same envelope the meetings
    feed uses, so a calendar can take it as a SECOND event source rather
    than needing the two merged server-side:

        calendar.addEventSource('/meetings/calendar/feed/');
        calendar.addEventSource('/messages/reminders/feed/');

    Reminders were missing from the calendar for the plain reason that
    nothing ever put them there — the meetings feed only knows about
    meetings. They are points in time, not spans, so each one is emitted
    as an all-day=false zero-length event at ``remind_at``.

    Returns reminders this user will actually receive: ones they created,
    plus ones addressed to them as an attendee.
    """

    def get(self, request):
        start = _parse_feed_date(request.GET.get('start'))
        end = _parse_feed_date(request.GET.get('end'))
        if start is None or end is None:
            now = timezone.now()
            start = start or (now - timedelta(days=31))
            end = end or (now + timedelta(days=93))

        rows = (ChatReminder.objects
                .filter(Q(owner=request.user) | Q(receipts__user=request.user))
                .filter(remind_at__gte=start, remind_at__lt=end)
                .exclude(status=ChatReminder.Status.CANCELLED)
                .select_related('room', 'owner')
                .distinct()
                .order_by('remind_at')[:500])

        # One query for this user's own receipt state, so a reminder they
        # have already resolved renders differently instead of sitting on
        # the calendar looking outstanding forever.
        states = dict(
            ChatReminderReceipt.objects
            .filter(user=request.user, reminder__in=[r.pk for r in rows])
            .values_list('reminder_id', 'state')
        )

        events = []
        for r in rows:
            state = states.get(r.pk, '')
            done = state in (ChatReminderReceipt.State.RESOLVED,
                             ChatReminderReceipt.State.DISMISSED)
            events.append({
                'id':        f'reminder-{r.pk}',
                'title':     ('✓ ' if done else '⏰ ') + r.title,
                'start':     timezone.localtime(r.remind_at).isoformat(),
                'allDay':    False,
                'url':       f'/messages/{r.room_id}/' if r.room_id else '',
                'color':     '#94a3b8' if done else '#f59e0b',
                'textColor': '#ffffff',
                'extendedProps': {
                    'kind':       'reminder',
                    'reminder_id': str(r.pk),
                    'note':       r.note,
                    'state':      state or r.status,
                    'resolved':   done,
                    'audience':   r.audience,
                    'source':     r.source,
                    'room_name':  r.room.name if r.room_id and r.room else '',
                    'meeting_id': str(r.meeting_id) if r.meeting_id else '',
                    'is_owner':   r.owner_id == request.user.id,
                },
            })

        return JsonResponse(events, safe=False)


def _parse_feed_date(value):
    """Accept the YYYY-MM-DD (or full ISO) that calendar widgets send."""
    if not value:
        return None
    parsed = parse_datetime(str(value))
    if parsed is None:
        try:
            from datetime import datetime
            parsed = datetime.strptime(str(value)[:10], '%Y-%m-%d')
        except (ValueError, TypeError):
            return None
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, timezone.get_current_timezone())
    return parsed


class ReminderCancelView(LoginRequiredMixin, View):
    """
    POST /messages/reminders/<reminder_id>/cancel/

    Only the creator can cancel the reminder itself — that pulls it from
    everyone. Recipients who merely want it off their own screen use
    resolve/dismiss instead.
    """

    def post(self, request, reminder_id):
        reminder = get_object_or_404(ChatReminder, id=reminder_id,
                                     owner=request.user)
        reminder.status = ChatReminder.Status.CANCELLED
        reminder.save(update_fields=['status'])
        ChatReminderReceipt.objects.filter(
            reminder=reminder,
            state__in=[ChatReminderReceipt.State.ACTIVE,
                       ChatReminderReceipt.State.SNOOZED],
        ).update(state=ChatReminderReceipt.State.DISMISSED,
                 resolved_at=timezone.now())
        return JsonResponse({'ok': True})
