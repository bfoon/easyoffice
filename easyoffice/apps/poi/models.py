"""
apps/poi/models.py
──────────────────
POIProfile  — 1-to-1 with core.User. Holds all Person-Of-Interest state so the
              User table stays clean.
POIInvite   — tokenised one-time registration link emailed at creation.

Trust model
-----------
* On successful OTP + device capture we set `trusted_until = now + 30 days`
  and bind the device via integrations.trust_device().
* `is_access_valid` is the single gate the portal guard checks every request.
* A daily task (management command / Celery) calls `expire_due()` which untrusts
  the device and disables the account once `trusted_until` passes.
"""

import uuid
from datetime import timedelta

from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _


TRUST_DAYS = 30


class POIProfile(models.Model):
    class Status(models.TextChoices):
        INVITED = "invited", _("Invited")          # created, not yet registered
        ACTIVE = "active", _("Active")              # registered + device trusted
        DISABLED = "disabled", _("Disabled")        # manually or auto disabled
        EXPIRED = "expired", _("Expired")           # 30-day trust lapsed

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.OneToOneField(
        "core.User", on_delete=models.CASCADE, related_name="poi_profile"
    )

    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.INVITED
    )

    # Authoritative trust window. Set when the POI completes OTP + device bind.
    trusted_until = models.DateTimeField(null=True, blank=True)

    # Fingerprint of the single device this POI is bound to.
    device_fingerprint = models.CharField(max_length=255, blank=True)

    # Private output folder (FileFolder) where this POI's tool outputs land.
    # Lazy FK string avoids importing the files app at module load.
    workspace_folder = models.ForeignKey(
        "files.FileFolder",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )

    created_by = models.ForeignKey(
        "core.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_pois",
    )
    disabled_by = models.ForeignKey(
        "core.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="disabled_pois",
    )
    disabled_at = models.DateTimeField(null=True, blank=True)
    disabled_reason = models.CharField(max_length=255, blank=True)

    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status", "trusted_until"]),
        ]

    def __str__(self):
        return f"POI<{self.user_id}:{self.status}>"

    # ── trust lifecycle ──────────────────────────────────────────────────────

    def activate(self, fingerprint, days=TRUST_DAYS):
        """Mark active and (re)start the trust window. Caller also binds the
        device via integrations.trust_device()."""
        self.device_fingerprint = fingerprint or ""
        self.trusted_until = timezone.now() + timedelta(days=days)
        self.status = self.Status.ACTIVE
        self.disabled_at = None
        self.disabled_reason = ""
        self.save(update_fields=[
            "device_fingerprint", "trusted_until", "status",
            "disabled_at", "disabled_reason", "updated_at",
        ])

    def disable(self, by=None, reason=""):
        """Disable the account + flip the underlying User inactive."""
        from apps.poi.integrations import untrust_all_devices

        self.status = self.Status.DISABLED
        self.disabled_by = by
        self.disabled_at = timezone.now()
        self.disabled_reason = reason or ""
        self.save(update_fields=[
            "status", "disabled_by", "disabled_at",
            "disabled_reason", "updated_at",
        ])
        untrust_all_devices(self.user)
        if self.user.is_active:
            self.user.is_active = False
            self.user.save(update_fields=["is_active"])

    def expire(self):
        """30-day lapse: untrust device + disable account, status=EXPIRED."""
        from apps.poi.integrations import untrust_all_devices

        self.status = self.Status.EXPIRED
        self.save(update_fields=["status", "updated_at"])
        untrust_all_devices(self.user)
        if self.user.is_active:
            self.user.is_active = False
            self.user.save(update_fields=["is_active"])

    @property
    def is_expired(self):
        return bool(self.trusted_until and timezone.now() >= self.trusted_until)

    @property
    def is_access_valid(self):
        """Single gate the portal guard checks. Active + within trust window."""
        return (
            self.status == self.Status.ACTIVE
            and self.user.is_active
            and not self.is_expired
        )

    def device_matches(self, fingerprint):
        return bool(
            self.device_fingerprint
            and fingerprint
            and self.device_fingerprint == fingerprint
        )

    # ── bulk expiry (called by the daily task) ───────────────────────────────

    @classmethod
    def expire_due(cls):
        """Expire every ACTIVE profile whose trust window has lapsed.
        Returns the number expired."""
        due = cls.objects.filter(
            status=cls.Status.ACTIVE,
            trusted_until__lt=timezone.now(),
        ).select_related("user")
        n = 0
        for prof in due:
            prof.expire()
            n += 1
        return n


class POIInvite(models.Model):
    """One-time tokenised registration link emailed when a POI is created."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    profile = models.ForeignKey(
        POIProfile, on_delete=models.CASCADE, related_name="invites"
    )
    token = models.UUIDField(default=uuid.uuid4, editable=False, unique=True, db_index=True)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"POIInvite<{self.token}>"

    @classmethod
    def issue(cls, profile, valid_hours=72):
        return cls.objects.create(
            profile=profile,
            expires_at=timezone.now() + timedelta(hours=valid_hours),
        )

    @property
    def is_valid(self):
        return self.used_at is None and timezone.now() < self.expires_at

    def mark_used(self):
        self.used_at = timezone.now()
        self.save(update_fields=["used_at"])


class POIRoomExtension(models.Model):
    """
    Marks a chat room as *deliberately* extended to include staff beyond the
    usual admin/CEO allowed-contacts, while still containing a POI.

    Why this exists
    ---------------
    The POI portal's default invariant is: a POI is only ever in a room with
    admins/CEO. That confinement is enforced in apps.poi.scoping. This model is
    the single, auditable exception: an admin/CEO may add named staff to a
    specific POI channel. Scoping consults this table so the exception is
    per-room and opt-in — it never loosens the rule for any other room.

    A row existing means: "for THIS room, the users in `allowed_extra` are
    permitted to share it with the POI." Removing the row (or emptying the set)
    instantly re-confines the room.

    Another POI is NEVER a valid extra member — that stays hard-blocked in the
    view that writes here, regardless of this table.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # The room being extended. Lazy string FK avoids importing messaging here.
    room = models.OneToOneField(
        "messaging.ChatRoom",
        on_delete=models.CASCADE,
        related_name="poi_extension",
    )

    # Staff explicitly granted access to this POI room (beyond admins/CEO).
    allowed_extra = models.ManyToManyField(
        "core.User",
        related_name="poi_extended_rooms",
        blank=True,
    )

    extended_by = models.ForeignKey(
        "core.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="poi_room_extensions_made",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"POIRoomExtension<{self.room_id}>"

    def extra_ids(self):
        return set(self.allowed_extra.values_list("id", flat=True))