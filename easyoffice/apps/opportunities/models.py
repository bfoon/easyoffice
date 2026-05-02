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
    country_filter = models.TextField(
        blank=True,
        default='',
        help_text=(
            "Optional. Comma-separated list of country names to include "
            "(e.g. 'Philippines, Kenya, Gambia'). Leave blank to accept all "
            "countries. Matching is case-insensitive and tolerates partial "
            "names. RSS items without an extracted country are skipped when "
            "this filter is set."
        )
    )
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

    def get_country_filter_list(self):
        """Return a normalised list of allowed country names (lowercase, stripped).

        Empty list means "no filter — accept all countries".
        """
        if not self.country_filter:
            return []
        parts = [p.strip().lower() for p in self.country_filter.split(',')]
        return [p for p in parts if p]

    def country_is_allowed(self, country):
        """Return True if `country` passes this source's country filter.

        Matching rules:
        - If no filter is configured, every country is allowed.
        - Match is case-insensitive.
        - A configured entry matches if it appears as a substring of the
          extracted country, OR vice versa. This tolerates minor variations
          like 'Philippines' vs 'PHILIPPINES' vs 'Philippines, Republic of'.
        - If a filter is configured but no country was extracted, the row
          is rejected (we can't confirm it's wanted).
        """
        allowed = self.get_country_filter_list()
        if not allowed:
            return True

        if not country:
            return False

        c = country.strip().lower()
        for entry in allowed:
            if entry in c or c in entry:
                return True
        return False


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