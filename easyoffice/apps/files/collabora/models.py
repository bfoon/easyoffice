"""
apps/files/collabora/models.py

Tracks live editing sessions so the EasyOffice UI can show presence
("3 people editing") on file cards.

Not a pessimistic lock — Collabora coordinates the actual collaborative
editing internally. These rows exist purely for the presence UI.
"""
import uuid
from django.db import models
from django.conf import settings
from django.utils import timezone


class CollaboraSession(models.Model):
    """
    One row per active user-on-file editing session.

    Lifecycle:
      - Created (or touched) when the editor page loads and on each
        presence ping from the browser (every 30s).
      - Marked ended=True when the browser fires a beforeunload ping,
        or pruned by a cleanup task if last_seen is older than 2 minutes.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    file = models.ForeignKey(
        'files.SharedFile',
        on_delete=models.CASCADE,
        related_name='collabora_sessions',
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='collabora_sessions',
    )

    # The permission that was granted at the time the editor was launched.
    # Stored here so we don't have to recompute it on every presence ping.
    permission = models.CharField(
        max_length=10,
        choices=[('view', 'View'), ('edit', 'Edit'), ('full', 'Full')],
        default='view',
    )

    # Collabora generates a unique session identifier; we store it so that
    # a single user opening the same file in two tabs produces two rows
    # (accurate presence) rather than one.
    collabora_session_id = models.CharField(max_length=64, blank=True)

    started_at = models.DateTimeField(auto_now_add=True)
    last_seen = models.DateTimeField(default=timezone.now)
    ended_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-last_seen']
        indexes = [
            models.Index(fields=['file', 'ended_at']),
            models.Index(fields=['last_seen']),
        ]

    def __str__(self):
        return f'{self.user} editing {self.file.name}'

    @property
    def is_active(self):
        if self.ended_at:
            return False
        # Consider stale after 2 minutes without a ping.
        return (timezone.now() - self.last_seen).total_seconds() < 120

    def touch(self):
        """Update last_seen; called on every presence ping."""
        self.last_seen = timezone.now()
        self.save(update_fields=['last_seen'])

    def end(self):
        """Mark the session as ended."""
        if self.ended_at:
            return
        self.ended_at = timezone.now()
        self.save(update_fields=['ended_at'])

    # ── Class helpers ────────────────────────────────────────────────────────

    @classmethod
    def active_for_file(cls, file):
        """Return a queryset of currently-active sessions for a file."""
        cutoff = timezone.now() - timezone.timedelta(seconds=120)
        return cls.objects.filter(
            file=file, ended_at__isnull=True, last_seen__gte=cutoff
        ).select_related('user')

    @classmethod
    def prune_stale(cls):
        """Mark old unpinged sessions as ended. Safe to call from cron."""
        cutoff = timezone.now() - timezone.timedelta(seconds=120)
        return cls.objects.filter(
            ended_at__isnull=True, last_seen__lt=cutoff
        ).update(ended_at=timezone.now())
