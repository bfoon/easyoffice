"""
apps/messaging/models_reminders.py
─────────────────────────────────
Reminders — a thing to be reminded of, at a time, for one or more people.

TWO MODELS, NOT ONE
───────────────────
``ChatReminder`` is the DEFINITION: what, when, and who it goes to.
``ChatReminderReceipt`` is one recipient's COPY of it: delivered, seen,
snoozed until, resolved.

That split matters because a reminder sent to a meeting's attendees is
not one shared thing — if you snooze the 09:00 stand-up reminder for ten
minutes, that must not push it back for everyone else, and your marking
it done must not clear it off their screens. Every per-person verb
(snooze, resolve, dismiss) therefore lives on the receipt, and the
reminder row itself is immutable once it has fired.

A personal reminder is simply the same shape with exactly one receipt.

SOFT LINKS
──────────
``meeting_id`` is a plain UUID, not a ForeignKey. The calendar lives in
its own module and its model is not imported here, so a hard FK would
make this file depend on it and force a migration in both apps whenever
either changed. A reminder that outlives its meeting is also perfectly
sensible ("you never wrote up Tuesday's minutes"), which an FK with
CASCADE would delete and an FK with PROTECT would block.

Import into apps/messaging/models.py so migrations pick it up:

    from .models_reminders import ChatReminder, ChatReminderReceipt  # noqa

then:  python manage.py makemigrations messaging && python manage.py migrate
"""

import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _


class ChatReminder(models.Model):
    """What to be reminded of, and when."""

    class Audience(models.TextChoices):
        ME        = 'me',        _('Only me')
        ATTENDEES = 'attendees', _('Everyone invited')

    class Status(models.TextChoices):
        PENDING   = 'pending',   _('Waiting to fire')
        FIRED     = 'fired',     _('Delivered')
        CANCELLED = 'cancelled', _('Cancelled')

    class Source(models.TextChoices):
        MANUAL  = 'manual',  _('Created by hand')
        MEETING = 'meeting', _('From a calendar invite')
        MEMO    = 'memo',    _('From a memo')
        MESSAGE = 'message', _('From a message')

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name='chat_reminders',
        help_text='Who created it. Always gets a receipt.')

    title = models.CharField(max_length=200)
    note = models.TextField(blank=True)

    # ── Context (all optional) ───────────────────────────────────────────
    room = models.ForeignKey(
        'messaging.ChatRoom', on_delete=models.CASCADE,
        null=True, blank=True, related_name='reminders')
    message = models.ForeignKey(
        'messaging.ChatMessage', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='reminders',
        help_text='The memo / invite / message this was raised from.')
    meeting_id = models.UUIDField(
        null=True, blank=True, db_index=True,
        help_text='Soft link to a calendar meeting — see module docstring.')
    source = models.CharField(max_length=12, choices=Source.choices,
                              default=Source.MANUAL)

    # ── Schedule ─────────────────────────────────────────────────────────
    remind_at = models.DateTimeField(db_index=True)
    audience = models.CharField(max_length=12, choices=Audience.choices,
                                default=Audience.ME)
    status = models.CharField(max_length=12, choices=Status.choices,
                              default=Status.PENDING, db_index=True)
    fired_at = models.DateTimeField(null=True, blank=True)

    # Also push it to email / mobile, not just the in-app popup.
    notify_email = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['remind_at']
        indexes = [
            # The sweep's query: pending rows whose time has come.
            models.Index(fields=['status', 'remind_at']),
            models.Index(fields=['owner', 'status']),
        ]

    def __str__(self):
        return f'{self.title} @ {self.remind_at:%Y-%m-%d %H:%M}'

    @property
    def is_due(self):
        return (self.status == self.Status.PENDING
                and self.remind_at <= timezone.now())


class ChatReminderReceipt(models.Model):
    """One recipient's copy: their snooze, their resolution, their state."""

    class State(models.TextChoices):
        PENDING  = 'pending',  _('Not yet delivered')
        ACTIVE   = 'active',   _('Showing')
        SNOOZED  = 'snoozed',  _('Snoozed')
        RESOLVED = 'resolved', _('Done')
        DISMISSED = 'dismissed', _('Dismissed')

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    reminder = models.ForeignKey(ChatReminder, on_delete=models.CASCADE,
                                 related_name='receipts')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                             related_name='chat_reminder_receipts')

    state = models.CharField(max_length=12, choices=State.choices,
                             default=State.PENDING, db_index=True)

    delivered_at = models.DateTimeField(null=True, blank=True)
    seen_at = models.DateTimeField(null=True, blank=True)
    snoozed_until = models.DateTimeField(null=True, blank=True, db_index=True)
    snooze_count = models.PositiveSmallIntegerField(default=0)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        unique_together = [('reminder', 'user')]
        ordering = ['-delivered_at']
        indexes = [
            # "what should be on this person's screen right now"
            models.Index(fields=['user', 'state']),
            # the snooze half of the sweep
            models.Index(fields=['state', 'snoozed_until']),
        ]

    def __str__(self):
        return f'{self.user} · {self.reminder_id} · {self.state}'

    @property
    def is_open(self):
        """Still wants the user's attention."""
        return self.state in (self.State.ACTIVE, self.State.SNOOZED)
