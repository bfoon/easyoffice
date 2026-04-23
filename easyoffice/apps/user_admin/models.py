"""
apps.user_admin.models
──────────────────────
Data model for company-wide user management (CEO / Superuser / Administration
group only). Three concerns are persisted here:

1. AccountInvitation
   One-email, one-use sign-up link. The admin fills in the employee's email
   and basic details; the system emails a tokenised URL. On click, the new
   user sets their own password and confirms their details.

2. RegistrationLink
   Bulk onboarding link. Admin creates a single URL that can be used up to
   `max_uses` times before `expires_at`. Useful for a hiring cohort.

3. TempPassword
   Tracks a just-issued temporary password so we can force the user to
   change it on next login (via the PasswordChangeRequiredMiddleware).

All three use short-lived uuid tokens; nothing sensitive is stored raw.
"""
import uuid
import secrets
from datetime import timedelta

from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.core.models import User


def _generate_token(nbytes=32):
    """URL-safe 43-char token by default; plenty of entropy for invite links."""
    return secrets.token_urlsafe(nbytes)


class AccountInvitation(models.Model):
    """
    A one-user, one-use invitation email. Admin creates it; the recipient
    clicks the link in their inbox to set up their account.
    """

    class Status(models.TextChoices):
        PENDING  = 'pending',  _('Pending')
        ACCEPTED = 'accepted', _('Accepted')
        REVOKED  = 'revoked',  _('Revoked')
        EXPIRED  = 'expired',  _('Expired')

    DEFAULT_EXPIRY_DAYS = 7

    id          = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    email       = models.EmailField()
    first_name  = models.CharField(max_length=150, blank=True)
    last_name   = models.CharField(max_length=150, blank=True)
    employee_id = models.CharField(max_length=50, blank=True)
    # Pre-assigned group (e.g. "Finance"). Optional, comma-separated names OK
    # for simplicity; looked up by name at acceptance time.
    group_names = models.CharField(max_length=300, blank=True)

    token        = models.CharField(max_length=64, unique=True, default=_generate_token)
    status       = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    invited_by   = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, related_name='sent_invitations',
    )
    accepted_by  = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='accepted_invitation',
    )
    expires_at   = models.DateTimeField()
    created_at   = models.DateTimeField(auto_now_add=True)
    accepted_at  = models.DateTimeField(null=True, blank=True)
    message      = models.TextField(blank=True, help_text='Personal note included in the email')

    class Meta:
        ordering = ['-created_at']
        indexes  = [models.Index(fields=['token']), models.Index(fields=['email'])]

    def __str__(self):
        return f'Invite({self.email}) — {self.status}'

    def save(self, *args, **kwargs):
        if not self.expires_at:
            self.expires_at = timezone.now() + timedelta(days=self.DEFAULT_EXPIRY_DAYS)
        super().save(*args, **kwargs)

    @property
    def is_active(self):
        return self.status == self.Status.PENDING and self.expires_at > timezone.now()

    def mark_expired_if_needed(self):
        if self.status == self.Status.PENDING and self.expires_at <= timezone.now():
            self.status = self.Status.EXPIRED
            self.save(update_fields=['status'])


class RegistrationLink(models.Model):
    """
    Shared sign-up link. N-of-M usage model: up to `max_uses` people can
    register before `expires_at`. Usable for new-hire cohorts, pilot groups,
    department-wide rollouts, etc.
    """

    class Status(models.TextChoices):
        ACTIVE   = 'active',   _('Active')
        REVOKED  = 'revoked',  _('Revoked')
        EXPIRED  = 'expired',  _('Expired')
        EXHAUSTED = 'exhausted', _('Fully used')

    DEFAULT_EXPIRY_DAYS = 7
    DEFAULT_MAX_USES    = 5

    id          = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    label       = models.CharField(max_length=150, help_text='For admins — e.g. "Jan 2026 Interns"')
    token       = models.CharField(max_length=64, unique=True, default=_generate_token)

    # Optional pre-assignments applied to every user registered via this link
    group_names = models.CharField(max_length=300, blank=True,
                                   help_text='Comma-separated group names — applied on registration')

    max_uses    = models.PositiveSmallIntegerField(default=DEFAULT_MAX_USES)
    use_count   = models.PositiveSmallIntegerField(default=0)
    expires_at  = models.DateTimeField()

    status      = models.CharField(max_length=20, choices=Status.choices, default=Status.ACTIVE)
    created_by  = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, related_name='created_registration_links',
    )
    created_at  = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        indexes  = [models.Index(fields=['token'])]

    def __str__(self):
        return f'{self.label} ({self.use_count}/{self.max_uses})'

    def save(self, *args, **kwargs):
        if not self.expires_at:
            self.expires_at = timezone.now() + timedelta(days=self.DEFAULT_EXPIRY_DAYS)
        super().save(*args, **kwargs)

    @property
    def remaining(self):
        return max(0, self.max_uses - self.use_count)

    @property
    def is_usable(self):
        """True if a new signup can currently use this link."""
        if self.status != self.Status.ACTIVE:
            return False
        if self.expires_at <= timezone.now():
            return False
        if self.use_count >= self.max_uses:
            return False
        return True

    def refresh_status(self):
        """Flip stale links from ACTIVE into the appropriate terminal state."""
        if self.status != self.Status.ACTIVE:
            return
        if self.expires_at <= timezone.now():
            self.status = self.Status.EXPIRED
            self.save(update_fields=['status'])
        elif self.use_count >= self.max_uses:
            self.status = self.Status.EXHAUSTED
            self.save(update_fields=['status'])


class RegistrationLinkUsage(models.Model):
    """One row per user that registered through a RegistrationLink."""
    link       = models.ForeignKey(RegistrationLink, on_delete=models.CASCADE, related_name='usages')
    user       = models.ForeignKey(User, on_delete=models.CASCADE, related_name='registration_link_usages')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [('link', 'user')]
        ordering = ['-created_at']


class TempPassword(models.Model):
    """
    Record of a temporary password issued for a user. Its existence with
    is_consumed=False flags that `user` MUST change their password on next
    login (enforced by PasswordChangeRequiredMiddleware).
    """
    id          = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user        = models.ForeignKey(User, on_delete=models.CASCADE, related_name='temp_passwords')
    # We never store the raw temp password after issuing — only a short hash
    # for audit / "issued a temp pw ending in ...xx" display. Full temp pw
    # is shown to the admin once in the UI and then forgotten server-side.
    password_hint = models.CharField(max_length=10, blank=True,
                                     help_text='Last 4 chars of the temp pw (for reference only)')
    issued_by   = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, related_name='issued_temp_passwords',
    )
    is_consumed = models.BooleanField(default=False)  # flips to True when user changes pw
    created_at  = models.DateTimeField(auto_now_add=True)
    consumed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'TempPW for {self.user.email} ({"used" if self.is_consumed else "pending"})'

    @classmethod
    def user_must_change_password(cls, user):
        """True if the user has any un-consumed temp password outstanding."""
        return cls.objects.filter(user=user, is_consumed=False).exists()

    @classmethod
    def mark_user_pw_changed(cls, user):
        """Called after a user successfully sets their own password."""
        cls.objects.filter(user=user, is_consumed=False).update(
            is_consumed=True, consumed_at=timezone.now(),
        )
