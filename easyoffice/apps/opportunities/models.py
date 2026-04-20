import uuid
from django.db import models
from django.conf import settings
from django.utils import timezone


class OpportunitySource(models.Model):
    SOURCE_TYPES = (
        ('html', 'HTML Page'),
        ('rss', 'RSS Feed'),
        ('undp_table', 'UNDP Table Page'),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=150)
    url = models.URLField(unique=True)
    source_type = models.CharField(max_length=20, choices=SOURCE_TYPES, default='html')
    is_active = models.BooleanField(default=True)
    scan_interval_minutes = models.PositiveIntegerField(default=180)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='created_opportunity_sources'
    )
    last_checked_at = models.DateTimeField(null=True, blank=True)
    last_status = models.CharField(max_length=50, blank=True)
    last_error = models.TextField(blank=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name


class OpportunityKeyword(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    keyword = models.CharField(max_length=150, unique=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ['keyword']

    def __str__(self):
        return self.keyword


class OpportunityWatcher(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='opportunity_watchers'
    )
    source = models.ForeignKey(
        OpportunitySource,
        on_delete=models.CASCADE,
        related_name='watchers'
    )
    keywords = models.ManyToManyField(OpportunityKeyword, blank=True)
    notify_email = models.BooleanField(default=True)
    notify_in_app = models.BooleanField(default=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        unique_together = ('user', 'source')
        ordering = ['user__first_name', 'user__last_name']

    def __str__(self):
        return f"{self.user} → {self.source.name}"


class OpportunityMatch(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    source = models.ForeignKey(
        OpportunitySource,
        on_delete=models.CASCADE,
        related_name='matches'
    )
    keyword = models.ForeignKey(
        OpportunityKeyword,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='matches'
    )

    title = models.CharField(max_length=300)
    summary = models.TextField(blank=True)
    external_url = models.URLField()

    reference_no = models.CharField(max_length=120, blank=True, db_index=True)
    country = models.CharField(max_length=150, blank=True, db_index=True)
    office = models.CharField(max_length=200, blank=True)
    procurement_process = models.CharField(max_length=200, blank=True)

    published_at = models.DateTimeField(null=True, blank=True)
    posted_date = models.DateTimeField(null=True, blank=True)
    detected_at = models.DateTimeField(auto_now_add=True)
    deadline = models.DateTimeField(null=True, blank=True)

    fingerprint = models.CharField(max_length=255, unique=True)
    is_task_created = models.BooleanField(default=False)
    is_read = models.BooleanField(default=False)

    class Meta:
        ordering = ['-detected_at']
        indexes = [
            models.Index(fields=['-detected_at']),
            models.Index(fields=['is_read']),
            models.Index(fields=['is_task_created']),
            models.Index(fields=['country']),
            models.Index(fields=['reference_no']),
        ]

    def __str__(self):
        return self.title

    @property
    def is_deadline_passed(self):
        return bool(self.deadline and self.deadline < timezone.now())

    @property
    def days_to_deadline(self):
        if not self.deadline:
            return None
        return (self.deadline - timezone.now()).days

    @property
    def is_deadline_soon(self):
        if not self.deadline:
            return False
        delta = self.deadline - timezone.now()
        return 0 <= delta.days <= 7

    @property
    def deadline_state(self):
        if not self.deadline:
            return 'none'
        if self.is_deadline_passed:
            return 'passed'
        if self.days_to_deadline is not None and self.days_to_deadline <= 2:
            return 'critical'
        if self.is_deadline_soon:
            return 'soon'
        return 'normal'