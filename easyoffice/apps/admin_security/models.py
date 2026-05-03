"""
apps/admin_security/models.py
─────────────────────────────
Two tiny models:

    AdminAccessSetting
        Singleton row. Holds the master kill-switch for the Django admin
        and the toggle for email OTP on admin login. Use
        AdminAccessSetting.load() everywhere — it caches the row and
        creates it lazily on first access.

    AdminEmailOTP
        One row per OTP we email out. We store a hashed code, the issue
        time, an expiry, an attempt counter, and a `consumed_at` flag so
        each code is good for exactly one successful use.
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta

from django.conf import settings
from django.db import models
from django.utils import timezone


# ── Settings singleton ─────────────────────────────────────────────────────

class AdminAccessSetting(models.Model):
    """
    There is only ever one row (pk=1). Use `AdminAccessSetting.load()`.
    """
    SINGLETON_ID = 1

    is_admin_enabled = models.BooleanField(
        default=True,
        help_text=(
            'Master switch. When OFF, the Django admin (/admin/) is closed '
            'to everyone except the superuser who turned it off (so you '
            "can't lock yourself out)."
        ),
    )
    require_otp_for_admin = models.BooleanField(
        default=False,
        help_text=(
            'When ON, every admin user must enter a 6-digit code emailed '
            'to them after submitting their password.'
        ),
    )
    otp_validity_minutes = models.PositiveSmallIntegerField(
        default=10,
        help_text='How long an emailed OTP stays valid.',
    )
    last_changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='+',
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Admin access setting'
        verbose_name_plural = 'Admin access settings'

    def __str__(self) -> str:
        return (
            f'AdminAccessSetting('
            f'enabled={self.is_admin_enabled}, '
            f'otp={self.require_otp_for_admin})'
        )

    # Singleton plumbing ───────────────────────────────────────────────────

    def save(self, *args, **kwargs):
        # Force pk=1 so we can never accidentally end up with two rows.
        self.pk = self.SINGLETON_ID
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        # Refuse — deleting it would lose the kill-switch state.
        return

    @classmethod
    def load(cls) -> 'AdminAccessSetting':
        obj, _ = cls.objects.get_or_create(pk=cls.SINGLETON_ID)
        return obj


# ── One-time email OTPs ────────────────────────────────────────────────────

def _hash_code(raw: str) -> str:
    """SHA-256 of the code. We never store the plain code."""
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


class AdminEmailOTP(models.Model):
    """
    A pending OTP issued to an admin user during login.

    Lifecycle:
        • Created when the user submits a valid password and OTP is required.
        • Consumed (consumed_at set) on the first correct entry.
        • Expires after `expires_at` regardless.

    We rate-limit verification with `attempts` so a code can't be brute-forced.
    """
    MAX_ATTEMPTS = 5

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='admin_email_otps',
    )
    code_hash = models.CharField(max_length=64)  # sha256 hex
    sent_to_email = models.EmailField()
    attempts = models.PositiveSmallIntegerField(default=0)
    consumed_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField()
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['user', 'consumed_at']),
        ]

    def __str__(self) -> str:
        return f'AdminEmailOTP(user={self.user_id}, expires={self.expires_at:%Y-%m-%d %H:%M})'

    # Helpers ──────────────────────────────────────────────────────────────

    @classmethod
    def issue_for(cls, user, *, validity_minutes: int = 10,
                  ip: str = '', user_agent: str = '') -> tuple['AdminEmailOTP', str]:
        """
        Build a fresh code for `user`, save the hashed version, return
        (otp_row, raw_six_digit_code). The caller is responsible for
        emailing `raw_six_digit_code`.

        Any prior un-consumed codes for this user are invalidated so a
        new one is the only valid one.
        """
        cls.objects.filter(user=user, consumed_at__isnull=True).update(
            consumed_at=timezone.now(),
        )

        # 6-digit code, zero-padded. secrets is cryptographically secure.
        raw = f'{secrets.randbelow(1_000_000):06d}'
        otp = cls.objects.create(
            user=user,
            code_hash=_hash_code(raw),
            sent_to_email=user.email or '',
            expires_at=timezone.now() + timedelta(minutes=max(1, validity_minutes)),
            ip_address=ip or None,
            user_agent=(user_agent or '')[:255],
        )
        return otp, raw

    @property
    def is_expired(self) -> bool:
        return timezone.now() >= self.expires_at

    @property
    def is_consumed(self) -> bool:
        return self.consumed_at is not None

    @property
    def is_locked(self) -> bool:
        return self.attempts >= self.MAX_ATTEMPTS

    def verify(self, raw_code: str) -> bool:
        """
        Check `raw_code` against the stored hash. Increments the attempts
        counter on every call, marks the row consumed on first success.
        """
        if self.is_consumed or self.is_expired or self.is_locked:
            return False
        # Always burn an attempt, even on success — keeps logic simple.
        self.attempts += 1
        if _hash_code(raw_code or '') == self.code_hash:
            self.consumed_at = timezone.now()
            self.save(update_fields=['attempts', 'consumed_at'])
            return True
        self.save(update_fields=['attempts'])
        return False
