import uuid
from django.db import models
from django.utils.translation import gettext_lazy as _
from apps.core.models import User


class TaskCategory(models.Model):
    name = models.CharField(max_length=100)
    color = models.CharField(max_length=7, default='#2196f3')
    icon = models.CharField(max_length=50, default='bi-tag')
    department = models.ForeignKey('organization.Department', on_delete=models.SET_NULL,
                                   null=True, blank=True)
    def __str__(self):
        return self.name


class Task(models.Model):
    class Priority(models.TextChoices):
        LOW = 'low', _('Low')
        MEDIUM = 'medium', _('Medium')
        HIGH = 'high', _('High')
        URGENT = 'urgent', _('Urgent')
        CRITICAL = 'critical', _('Critical')

    class Status(models.TextChoices):
        TODO = 'todo', _('To Do')
        IN_PROGRESS = 'in_progress', _('In Progress')
        REVIEW = 'review', _('Under Review')
        ON_HOLD = 'on_hold', _('On Hold')
        DONE = 'done', _('Done')
        CANCELLED = 'cancelled', _('Cancelled')

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    title = models.CharField(max_length=300)
    description = models.TextField(blank=True)
    assigned_to = models.ForeignKey(User, on_delete=models.CASCADE, related_name='assigned_tasks')
    assigned_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name='created_tasks')
    collaborators = models.ManyToManyField(User, related_name='collaborative_tasks', blank=True)
    category = models.ForeignKey(TaskCategory, on_delete=models.SET_NULL, null=True, blank=True)
    project = models.ForeignKey('projects.Project', on_delete=models.SET_NULL, null=True, blank=True,
                                related_name='tasks')
    parent_task = models.ForeignKey('self', on_delete=models.SET_NULL, null=True, blank=True,
                                    related_name='subtasks')
    priority = models.CharField(max_length=20, choices=Priority.choices, default=Priority.MEDIUM)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.TODO)
    due_date = models.DateTimeField(null=True, blank=True)
    start_date = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    estimated_hours = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    actual_hours = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    progress_pct = models.PositiveSmallIntegerField(default=0)
    tags = models.CharField(max_length=500, blank=True)
    attachments_count = models.PositiveIntegerField(default=0)
    is_private = models.BooleanField(default=False)
    is_recurring = models.BooleanField(default=False)
    recurrence_rule = models.JSONField(null=True, blank=True)
    on_site_at = models.DateTimeField(null=True, blank=True)
    on_site_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='tasks_marked_on_site',
    )
    customer_visible = models.BooleanField(default=False)
    awaiting_cs_verification = models.BooleanField(default=False)

    # ── Pinning (focus for CEO / Admin / supervisors) ────────────────────
    is_pinned = models.BooleanField(default=False, db_index=True)
    pinned_at = models.DateTimeField(null=True, blank=True)
    pinned_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='tasks_pinned',
    )

    # ── Reminder bookkeeping ─────────────────────────────────────────────
    # Last time the reminder cron sent ANY reminder (pre-due or overdue)
    last_reminder_at = models.DateTimeField(null=True, blank=True)
    # Did we already send the "24h before due" warning? Avoid duplicates.
    pre_due_reminder_sent = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['assigned_to', 'status']),
            models.Index(fields=['due_date']),
            models.Index(fields=['-is_pinned', '-pinned_at']),
        ]

    def __str__(self):
        return self.title

    @property
    def is_overdue(self):
        from django.utils import timezone
        return self.due_date and self.due_date < timezone.now() and self.status not in ('done', 'cancelled')

    def mark_on_site(self, *, by_user, gps=None, note=''):
        """
        Record that a technician arrived on site. Idempotent: if already
        on site, this is a no-op (we don't overwrite the original
        timestamp — first-arrive wins).

        Side-effects (notifications, OnSiteEvent row) are NOT done here
        — they're done by the calling view, after this method commits,
        so model code stays free of side effects.
        """
        from django.utils import timezone
        if self.on_site_at:
            return False  # already on site
        self.on_site_at = timezone.now()
        self.on_site_by = by_user
        self.save(update_fields=['on_site_at', 'on_site_by', 'updated_at'])
        return True

    def clear_on_site(self):
        """Technician taps 'I've left site'. Doesn't delete the OnSiteEvent
        row — that history stays. Just clears the live flag."""
        if not self.on_site_at:
            return False
        self.on_site_at = None
        self.on_site_by = None
        self.save(update_fields=['on_site_at', 'on_site_by', 'updated_at'])
        return True

    def mark_done_pending_verify(self):
        """
        Used INSTEAD of setting status='done' directly when:
          - the task has a linked customer-service assignment, or
          - the task is customer_visible.

        This flips the task to status='review' and sets
        awaiting_cs_verification=True. The CS owner then confirms with
        the customer and closes the ticket the normal way.
        """
        from django.utils import timezone
        self.status = self.Status.REVIEW
        self.awaiting_cs_verification = True
        self.completed_at = timezone.now()
        self.progress_pct = 100
        self.save(update_fields=[
            'status', 'awaiting_cs_verification',
            'completed_at', 'progress_pct', 'updated_at',
        ])

    @property
    def has_activity(self):
        """
        True if there's content on the task that would be lost on delete.

        We only care about user-contributed CONTENT here:
          - any comment
          - any attachment (file or photo)

        Things that DON'T count as activity (task is still deletable):
          - time-logs
          - reassignments
          - status changes / progress updates
          - on-site arrival
          - pin / unpin

        Rationale: comments and attachments are work product authored by
        people other than (or including) the creator. Status/time/pin
        changes are administrative and the creator is allowed to throw
        the task away.
        """
        if self.comments.exists():
            return True
        if self.attachments.exists():
            return True
        return False

    def pin(self, *, by_user):
        """Pin this task to the top of focus lists."""
        from django.utils import timezone
        if self.is_pinned:
            return False
        self.is_pinned = True
        self.pinned_at = timezone.now()
        self.pinned_by = by_user
        self.save(update_fields=['is_pinned', 'pinned_at', 'pinned_by', 'updated_at'])
        return True

    def unpin(self):
        if not self.is_pinned:
            return False
        self.is_pinned = False
        self.pinned_at = None
        self.pinned_by = None
        self.save(update_fields=['is_pinned', 'pinned_at', 'pinned_by', 'updated_at'])
        return True


class TaskComment(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name='comments')
    author = models.ForeignKey(User, on_delete=models.CASCADE)
    content = models.TextField()
    parent = models.ForeignKey('self', on_delete=models.SET_NULL, null=True, blank=True, related_name='replies')
    is_edited = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['created_at']

    def __str__(self):
        return f'Comment by {self.author} on {self.task}'


class TaskAttachment(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name='attachments')
    uploaded_by = models.ForeignKey(User, on_delete=models.CASCADE)
    file = models.FileField(upload_to='task_attachments/%Y/%m/')
    file_name = models.CharField(max_length=255)
    file_size = models.PositiveIntegerField(default=0)
    file_type = models.CharField(max_length=100, blank=True)
    uploaded_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.file_name

    @property
    def is_image(self):
        """True if the attachment is an image — used by the template to
        decide between rendering a thumbnail vs a generic file icon."""
        if self.file_type and self.file_type.startswith('image/'):
            return True
        name = (self.file_name or '').lower()
        return name.endswith(('.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp'))

    @property
    def human_size(self):
        """Human-readable file size: '482 B', '1.4 MB', '2.3 GB' etc."""
        size = self.file_size or 0
        for unit in ('B', 'KB', 'MB', 'GB'):
            if size < 1024:
                return f"{size:.0f} {unit}" if unit == 'B' else f"{size:.1f} {unit}"
            size /= 1024
        return f"{size:.1f} TB"


class TaskReassignment(models.Model):
    """Track full or partial task reassignments."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name='reassignments')
    from_user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='tasks_reassigned_from')
    to_user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='tasks_reassigned_to')
    reassigned_by = models.ForeignKey(User, on_delete=models.CASCADE, related_name='tasks_reassigned_by')
    is_full_reassignment = models.BooleanField(default=True)
    portion_description = models.TextField(blank=True)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)


class TaskTimeLog(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name='time_logs')
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    hours = models.DecimalField(max_digits=5, decimal_places=2)
    description = models.TextField(blank=True)
    date = models.DateField()
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f'{self.user} — {self.hours}h on {self.task}'