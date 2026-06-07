"""
apps/customer_portal/models_technician_portal.py
=================================================

NEW MODULE — tokenized, OTP-gated TECHNICIAN portal.

Parallels the customer portal's token + device-binding + OTP pattern, but
for staff technicians who log on-site visit entries from the field.

Models
------
  * TechnicianPortalToken   — ONE permanent link per technician. The token
      is bound to a technician's email on first use.
  * TechnicianTrustedDevice — a device that has passed OTP. Trust lasts
      `TRUST_DAYS` (default 30); after that, OTP is required again.
  * TechnicianOTP           — short-lived one-time codes for device
      verification.

Wire-up
-------
Add to the bottom of apps/customer_portal/models.py:

    from apps.customer_portal.models_technician_portal import (  # noqa
        TechnicianPortalToken, TechnicianTrustedDevice, TechnicianOTP,
    )

Then makemigrations + migrate.

Note on the email rule you specified:
  * The technician enters their email ONLY the first time they open the
    link (binding the token). After that the email is fixed; we never ask
    again. Device trust (not email) is what gates subsequent visits.
"""
from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import timedelta

from django.db import models
from django.utils import timezone

from apps.core.models import User


# How long a verified device stays trusted before OTP is needed again.
TRUST_DAYS = 30
# OTP lifetime.
OTP_TTL_MINUTES = 10
# How many OTP attempts before a code is burned.
OTP_MAX_ATTEMPTS = 5


def _gen_token() -> str:
    """URL-safe, unguessable token for the permanent technician link."""
    return secrets.token_urlsafe(32)


def _gen_otp() -> str:
    """6-digit numeric OTP."""
    return f'{secrets.randbelow(1_000_000):06d}'


def hash_device_id(raw: str) -> str:
    """Stable hash of the device cookie value (we never store it raw)."""
    return hashlib.sha256((raw or '').encode('utf-8')).hexdigest()


# ─────────────────────────────────────────────────────────────────────────────
# Token (one permanent link per technician)
# ─────────────────────────────────────────────────────────────────────────────

class TechnicianPortalToken(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    token = models.CharField(
        max_length=64, unique=True, default=_gen_token, db_index=True,
    )

    # The staff user this link belongs to. Set when the link is issued.
    technician = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        related_name='technician_portal_token',
    )

    # Email is captured on FIRST open and then locked. Until then it's blank.
    bound_email = models.EmailField(blank=True)
    email_bound_at = models.DateTimeField(null=True, blank=True)

    # Permanent link, but revocable for offboarding / lost-link cases.
    is_active = models.BooleanField(default=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        who = self.technician.full_name if self.technician_id else '—'
        return f'TechToken({who})'

    @property
    def is_email_bound(self) -> bool:
        return bool(self.bound_email)

    @property
    def resolved_email(self) -> str:
        """Bound email if set, else the technician's account email."""
        return self.bound_email or (self.technician.email if self.technician_id else '')

    def bind_email(self, email: str):
        self.bound_email = (email or '').strip().lower()[:254]
        self.email_bound_at = timezone.now()
        self.save(update_fields=['bound_email', 'email_bound_at'])

    def touch(self):
        self.last_used_at = timezone.now()
        self.save(update_fields=['last_used_at'])

    def revoke(self):
        self.is_active = False
        self.revoked_at = timezone.now()
        self.save(update_fields=['is_active', 'revoked_at'])


# ─────────────────────────────────────────────────────────────────────────────
# Trusted device
# ─────────────────────────────────────────────────────────────────────────────

class TechnicianTrustedDevice(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    token = models.ForeignKey(
        TechnicianPortalToken,
        on_delete=models.CASCADE,
        related_name='trusted_devices',
    )
    # We store only a hash of the device cookie, never the raw value.
    device_hash = models.CharField(max_length=64, db_index=True)

    device_label = models.CharField(max_length=255, blank=True)
    last_ip = models.GenericIPAddressField(null=True, blank=True)

    trusted_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    last_seen_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-last_seen_at']
        constraints = [
            models.UniqueConstraint(
                fields=['token', 'device_hash'],
                name='uniq_tech_token_device',
            ),
        ]

    def __str__(self):
        return f'TrustedDevice({self.device_label or self.device_hash[:8]})'

    @property
    def is_valid(self) -> bool:
        return self.expires_at and self.expires_at > timezone.now()

    def renew(self):
        self.expires_at = timezone.now() + timedelta(days=TRUST_DAYS)
        self.save(update_fields=['expires_at', 'last_seen_at'])

    @classmethod
    def grant(cls, *, token, device_hash, device_label='', ip=None):
        obj, _ = cls.objects.update_or_create(
            token=token, device_hash=device_hash,
            defaults={
                'device_label': (device_label or '')[:255],
                'last_ip': ip,
                'expires_at': timezone.now() + timedelta(days=TRUST_DAYS),
            },
        )
        return obj

    @classmethod
    def is_trusted(cls, *, token, device_hash) -> bool:
        obj = cls.objects.filter(token=token, device_hash=device_hash).first()
        if obj and obj.is_valid:
            obj.renew()
            return True
        return False


# ─────────────────────────────────────────────────────────────────────────────
# OTP
# ─────────────────────────────────────────────────────────────────────────────

class TechnicianOTP(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    token = models.ForeignKey(
        TechnicianPortalToken,
        on_delete=models.CASCADE,
        related_name='otps',
    )
    device_hash = models.CharField(max_length=64, db_index=True)

    code = models.CharField(max_length=6, default=_gen_otp)
    sent_to = models.EmailField()

    attempts = models.PositiveSmallIntegerField(default=0)
    consumed_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField()

    created_at = models.DateTimeField(auto_now_add=True)
    requested_ip = models.GenericIPAddressField(null=True, blank=True)
    device_label = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'OTP({self.sent_to}, {"used" if self.consumed_at else "live"})'

    @property
    def is_expired(self) -> bool:
        return self.expires_at and self.expires_at <= timezone.now()

    @property
    def is_live(self) -> bool:
        return (
            self.consumed_at is None
            and not self.is_expired
            and self.attempts < OTP_MAX_ATTEMPTS
        )

    def consume(self):
        self.consumed_at = timezone.now()
        self.save(update_fields=['consumed_at'])

    @classmethod
    def issue(cls, *, token, device_hash, sent_to, ip=None, device_label=''):
        """Create a fresh OTP, invalidating any prior live ones for this device."""
        cls.objects.filter(
            token=token, device_hash=device_hash, consumed_at__isnull=True,
        ).update(consumed_at=timezone.now())

        return cls.objects.create(
            token=token,
            device_hash=device_hash,
            sent_to=sent_to,
            requested_ip=ip,
            device_label=(device_label or '')[:255],
            expires_at=timezone.now() + timedelta(minutes=OTP_TTL_MINUTES),
        )

    @classmethod
    def verify(cls, *, token, device_hash, code) -> tuple[bool, str]:
        """
        Returns (ok, reason). On success, the OTP is consumed.
        """
        otp = (
            cls.objects
            .filter(token=token, device_hash=device_hash, consumed_at__isnull=True)
            .order_by('-created_at')
            .first()
        )
        if otp is None:
            return False, 'No active code. Please request a new one.'
        if otp.is_expired:
            return False, 'This code has expired. Please request a new one.'
        if otp.attempts >= OTP_MAX_ATTEMPTS:
            return False, 'Too many attempts. Please request a new code.'

        otp.attempts += 1
        otp.save(update_fields=['attempts'])

        if (code or '').strip() != otp.code:
            return False, 'That code is incorrect.'

        otp.consume()
        return True, ''
