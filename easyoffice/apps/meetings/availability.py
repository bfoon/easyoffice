"""
apps/meetings/availability.py
─────────────────────────────
One answer to "is this person in a meeting right now?", and one queryset
for "what is actually on the calendar between these two dates".

WHY THIS FILE EXISTS
────────────────────
The busy badge was staying on after a meeting had been ended. The cause
was not the badge — it was that "is this meeting happening" was being
worked out in several places, each from ``start_datetime`` and
``end_datetime``, neither of which changes when somebody presses End. A
two-hour meeting ended after twenty minutes therefore went on reading as
live for another hour and forty, in every one of those places
independently.

``Meeting.ended_at`` (added alongside this file) records the real end.
This module is the only thing that should be consulted about current
availability, so the rule lives in exactly one place and the next
feature that needs it cannot get a different answer.

USAGE
─────
    from apps.meetings.availability import is_in_meeting, current_meeting

    if is_in_meeting(user):
        ...

    meeting = current_meeting(user)      # None, or the Meeting
    if meeting:
        label = f'In a meeting until {meeting.effective_end:%H:%M}'

For the calendar feed:

    from apps.meetings.availability import calendar_queryset
    qs = calendar_queryset(request.user, start, end)
"""

import logging

from django.db.models import Q
from django.utils import timezone

from apps.meetings.models import Meeting

log = logging.getLogger(__name__)

# Statuses that can never occupy time, whatever the clock says.
DEAD_STATUSES = ('cancelled',)


def live_meetings_qs(at=None):
    """
    Every meeting genuinely in progress at *at* (default now).

    ``ended_at__isnull=True`` OR ``ended_at__gt=at`` is the important
    clause: a meeting that has been ended is over from that instant, not
    from its booked end.
    """
    at = at or timezone.now()
    return (Meeting.objects
            .filter(start_datetime__lte=at, end_datetime__gt=at)
            .filter(Q(ended_at__isnull=True) | Q(ended_at__gt=at))
            .exclude(status__in=('completed', 'cancelled')))


def current_meeting(user, at=None):
    """
    The meeting this user is actually sitting in, or None.

    Only counts people who said yes or are the organiser — being invited
    to something you declined should not mark you busy.
    """
    if not user or not getattr(user, 'is_authenticated', False):
        return None
    at = at or timezone.now()
    try:
        return (live_meetings_qs(at)
                .filter(Q(organizer=user)
                        | Q(attendees__user=user,
                            attendees__rsvp__in=('accepted', 'tentative')))
                .select_related('organizer')
                .order_by('start_datetime')
                .first())
    except Exception:
        log.exception('availability: current_meeting failed for %s',
                      getattr(user, 'pk', '?'))
        return None


def is_in_meeting(user, at=None):
    return current_meeting(user, at) is not None


def availability(user, at=None):
    """
    A small dict the UI can render directly.

        {'busy': True, 'reason': 'In a meeting', 'until': datetime,
         'meeting_id': '…', 'title': '…'}
    """
    meeting = current_meeting(user, at)
    if meeting is None:
        return {'busy': False, 'reason': '', 'until': None,
                'meeting_id': '', 'title': ''}
    return {
        'busy': True,
        'reason': 'In a meeting',
        'until': meeting.effective_end,
        'meeting_id': str(meeting.pk),
        'title': meeting.title if not meeting.is_private else 'Busy',
    }


def calendar_queryset(user, start, end, include_finished=True):
    """
    Meetings to draw on a calendar between *start* and *end*.

    ``include_finished`` keeps completed meetings visible — a calendar is
    a record as much as a plan, and a past week with everything erased is
    not useful. What it must NOT do is let them keep their full booked
    slot: draw a finished meeting from ``start_datetime`` to
    ``effective_end`` so the block shrinks to what actually happened.

    Cancelled meetings are always excluded. They did not happen and they
    are not going to.
    """
    qs = (Meeting.objects
          .filter(Q(organizer=user) | Q(attendees__user=user) | Q(is_private=False))
          .filter(start_datetime__lt=end, end_datetime__gt=start)
          .exclude(status__in=DEAD_STATUSES)
          .select_related('organizer', 'project')
          .distinct())
    if not include_finished:
        qs = qs.exclude(status='completed')
    return qs
