import uuid
from django.db import models
from django.utils.translation import gettext_lazy as _
from apps.core.models import User


class Meeting(models.Model):

    class MeetingType(models.TextChoices):
        ONE_ON_ONE  = 'one_on_one',  _('One-on-One')
        TEAM        = 'team',        _('Team Meeting')
        UNIT        = 'unit',        _('Unit Meeting')
        DEPARTMENT  = 'department',  _('Department Meeting')
        ALL_HANDS   = 'all_hands',   _('All-Hands')
        EXTERNAL    = 'external',    _('External')

    class Status(models.TextChoices):
        SCHEDULED   = 'scheduled',   _('Scheduled')
        IN_PROGRESS = 'in_progress', _('In Progress')
        COMPLETED   = 'completed',   _('Completed')
        CANCELLED   = 'cancelled',   _('Cancelled')

    id             = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    title          = models.CharField(max_length=300)
    description    = models.TextField(blank=True)
    meeting_type   = models.CharField(max_length=20, choices=MeetingType.choices,
                                      default=MeetingType.TEAM)
    status         = models.CharField(max_length=20, choices=Status.choices,
                                      default=Status.SCHEDULED)
    organizer      = models.ForeignKey(User, on_delete=models.CASCADE,
                                       related_name='organised_meetings')
    start_datetime = models.DateTimeField()
    end_datetime   = models.DateTimeField()
    location       = models.CharField(max_length=300, blank=True,
                                      help_text='Room name, address, or "Virtual"')
    virtual_link   = models.URLField(blank=True,
                                     help_text='Zoom / Teams / Meet link')
    # Links
    project        = models.ForeignKey('projects.Project', on_delete=models.SET_NULL,
                                       null=True, blank=True, related_name='meetings')
    linked_tasks   = models.ManyToManyField('tasks.Task', blank=True,
                                            related_name='meetings')
    # Scope helpers (used when type = unit / department / team)
    unit           = models.ForeignKey('organization.Unit', on_delete=models.SET_NULL,
                                       null=True, blank=True)
    department     = models.ForeignKey('organization.Department', on_delete=models.SET_NULL,
                                       null=True, blank=True)
    agenda         = models.TextField(blank=True)
    is_private     = models.BooleanField(default=False)
    created_at     = models.DateTimeField(auto_now_add=True)
    updated_at     = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-start_datetime']

    def __str__(self):
        return self.title

    @property
    def duration_minutes(self):
        if self.start_datetime and self.end_datetime:
            delta = self.end_datetime - self.start_datetime
            return int(delta.total_seconds() / 60)
        return 0

    @property
    def is_past(self):
        from django.utils import timezone
        return self.end_datetime < timezone.now()

    @property
    def type_icon(self):
        return {
            'one_on_one':  'bi-person-lines-fill',
            'team':        'bi-people-fill',
            'unit':        'bi-diagram-3-fill',
            'department':  'bi-building',
            'all_hands':   'bi-megaphone-fill',
            'external':    'bi-globe',
        }.get(self.meeting_type, 'bi-calendar-event')

    @property
    def type_color(self):
        return {
            'one_on_one':  '#8b5cf6',
            'team':        '#3b82f6',
            'unit':        '#10b981',
            'department':  '#f59e0b',
            'all_hands':   '#ef4444',
            'external':    '#64748b',
        }.get(self.meeting_type, '#3b82f6')


class MeetingAttendee(models.Model):

    class RSVP(models.TextChoices):
        INVITED    = 'invited',    _('Invited')
        ACCEPTED   = 'accepted',   _('Accepted')
        DECLINED   = 'declined',   _('Declined')
        TENTATIVE  = 'tentative',  _('Tentative')
        ATTENDED   = 'attended',   _('Attended')
        NO_SHOW    = 'no_show',    _('No Show')

    id          = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    meeting     = models.ForeignKey(Meeting, on_delete=models.CASCADE,
                                    related_name='attendees')
    user        = models.ForeignKey(User, on_delete=models.CASCADE,
                                    related_name='meeting_invites')
    rsvp        = models.CharField(max_length=20, choices=RSVP.choices,
                                   default=RSVP.INVITED)
    is_required = models.BooleanField(default=True)
    role        = models.CharField(max_length=100, blank=True,
                                   help_text='e.g. Presenter, Note-taker, Observer')
    notes       = models.TextField(blank=True)
    responded_at= models.DateTimeField(null=True, blank=True)
    checked_in_at = models.DateTimeField(null=True, blank=True)
    created_at  = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [('meeting', 'user')]
        ordering = ['user__first_name']

    def __str__(self):
        return f'{self.user.full_name} @ {self.meeting}'

    @property
    def rsvp_color(self):
        return {
            'accepted':  '#10b981',
            'declined':  '#ef4444',
            'tentative': '#f59e0b',
            'attended':  '#3b82f6',
            'no_show':   '#94a3b8',
        }.get(self.rsvp, '#64748b')

class MeetingExternalAttendee(models.Model):
    class RSVP(models.TextChoices):
        INVITED    = 'invited',    _('Invited')
        ACCEPTED   = 'accepted',   _('Accepted')
        DECLINED   = 'declined',   _('Declined')
        TENTATIVE  = 'tentative',  _('Tentative')
        ATTENDED   = 'attended',   _('Attended')
        NO_SHOW    = 'no_show',    _('No Show')

    id           = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    meeting      = models.ForeignKey(Meeting, on_delete=models.CASCADE, related_name='external_attendees')
    full_name    = models.CharField(max_length=255)
    email        = models.EmailField()
    organisation = models.CharField(max_length=255, blank=True)
    role         = models.CharField(max_length=100, blank=True, help_text='e.g. Client, Consultant, Donor')
    rsvp         = models.CharField(max_length=20, choices=RSVP.choices, default=RSVP.INVITED)
    is_required  = models.BooleanField(default=True)
    notes        = models.TextField(blank=True)
    responded_at = models.DateTimeField(null=True, blank=True)
    checked_in_at = models.DateTimeField(null=True, blank=True)
    created_at   = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [('meeting', 'email')]
        ordering = ['full_name']

    def __str__(self):
        return f'{self.full_name} @ {self.meeting}'

    @property
    def rsvp_color(self):
        return {
            'accepted':  '#10b981',
            'declined':  '#ef4444',
            'tentative': '#f59e0b',
            'attended':  '#3b82f6',
            'no_show':   '#94a3b8',
        }.get(self.rsvp, '#64748b')

class MeetingMinutes(models.Model):
    id          = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    meeting     = models.OneToOneField(Meeting, on_delete=models.CASCADE,
                                       related_name='minutes')
    author      = models.ForeignKey(User, on_delete=models.SET_NULL, null=True)
    content     = models.TextField(blank=True,
                                   help_text='Main notes body (supports Markdown-like syntax)')
    decisions   = models.TextField(blank=True,
                                   help_text='Key decisions made')
    is_final    = models.BooleanField(default=False)
    # When finalised the PDF is saved here AND as a SharedFile
    pdf_file    = models.ForeignKey('files.SharedFile', on_delete=models.SET_NULL,
                                    null=True, blank=True, related_name='meeting_minutes')
    finalised_at= models.DateTimeField(null=True, blank=True)
    created_at  = models.DateTimeField(auto_now_add=True)
    updated_at  = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f'Minutes — {self.meeting}'


class MeetingActionItem(models.Model):
    id          = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    minutes     = models.ForeignKey(MeetingMinutes, on_delete=models.CASCADE,
                                    related_name='action_items')
    description = models.CharField(max_length=500)
    assigned_to = models.ForeignKey(User, on_delete=models.SET_NULL,
                                    null=True, blank=True, related_name='meeting_action_items')
    due_date    = models.DateField(null=True, blank=True)
    is_done     = models.BooleanField(default=False)
    linked_task = models.ForeignKey('tasks.Task', on_delete=models.SET_NULL,
                                    null=True, blank=True)
    created_at  = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at']

    def __str__(self):
        return self.description
