"""
apps/logistics/models.py
========================

Logistics / delivery tracking.

Monitors the movement of purchased goods from the office to the customer
site, gives drivers a tokenized portal (with OTP device verification, like
the technician portal), tracks live GPS, and captures customer sign-off on
delivery.

Models
------
  Vehicle           — company-owned OR hired-for-delivery vehicles.
  Driver            — a delivery driver (may be staff or an external hire).
  DriverPortalToken — permanent tokenized link for the driver portal.
  DriverTrustedDevice / DriverOTP — device-trust + OTP (30-day trust).
  Shipment          — one delivery run office → customer site, linked to a
                      purchase order / invoice.
  ShipmentItem      — line items on the shipment (what's being delivered).
  ShipmentEvent     — status timeline (created, dispatched, en route, …).
  LocationPing      — live GPS breadcrumbs posted by the driver portal.
  ProofOfDelivery   — customer sign-off (typed/drawn signature + photo).
"""
from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import timedelta

from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.core.models import User


TRUST_DAYS = 30
OTP_TTL_MINUTES = 10
OTP_MAX_ATTEMPTS = 5


def _gen_token() -> str:
    return secrets.token_urlsafe(32)


def _gen_otp() -> str:
    return f'{secrets.randbelow(1_000_000):06d}'


def hash_device_id(raw: str) -> str:
    return hashlib.sha256((raw or '').encode('utf-8')).hexdigest()


# ─────────────────────────────────────────────────────────────────────────────
# Vehicle
# ─────────────────────────────────────────────────────────────────────────────

class Vehicle(models.Model):
    class Ownership(models.TextChoices):
        COMPANY = 'company', _('Company-owned')
        HIRED = 'hired', _('Hired')

    class Status(models.TextChoices):
        AVAILABLE = 'available', _('Available')
        ON_DELIVERY = 'on_delivery', _('On Delivery')
        MAINTENANCE = 'maintenance', _('In Maintenance')
        RETIRED = 'retired', _('Retired')

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    label = models.CharField(max_length=120, help_text='e.g. "Toyota Hilux — GAM 1234"')
    registration = models.CharField(max_length=40, blank=True, help_text='Plate / reg number')
    make = models.CharField(max_length=80, blank=True)
    model = models.CharField(max_length=80, blank=True)
    capacity_note = models.CharField(max_length=120, blank=True, help_text='e.g. "1 tonne", "12 boxes"')

    ownership = models.CharField(max_length=10, choices=Ownership.choices, default=Ownership.COMPANY)
    status = models.CharField(max_length=14, choices=Status.choices, default=Status.AVAILABLE, db_index=True)

    # Hired-vehicle details (ignored for company vehicles)
    hire_vendor = models.CharField(max_length=160, blank=True, help_text='Transport company / owner name')
    hire_contact = models.CharField(max_length=80, blank=True)
    hire_rate_note = models.CharField(max_length=120, blank=True, help_text='e.g. "D2,500 per trip"')

    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['label']

    def __str__(self):
        tag = '(hired)' if self.ownership == self.Ownership.HIRED else ''
        return f'{self.label} {tag}'.strip()

    @property
    def is_hired(self) -> bool:
        return self.ownership == self.Ownership.HIRED


# ─────────────────────────────────────────────────────────────────────────────
# Driver + portal access
# ─────────────────────────────────────────────────────────────────────────────

class Driver(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    full_name = models.CharField(max_length=160)
    phone = models.CharField(max_length=40, blank=True)
    email = models.EmailField(blank=True, help_text='Used for OTP delivery if the portal is enabled.')

    # A driver MAY be a staff user (company driver) or purely external (hired).
    staff_user = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='driver_profiles',
        help_text='Link to a staff account if this is an internal driver.',
    )
    is_external = models.BooleanField(
        default=False,
        help_text='True for hired drivers who are not employees.',
    )

    is_active = models.BooleanField(default=True)
    license_number = models.CharField(max_length=80, blank=True)
    notes = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['full_name']

    def __str__(self):
        return self.full_name + (' (hired)' if self.is_external else '')


class DriverPortalToken(models.Model):
    """Permanent tokenized link for a driver's delivery portal."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    token = models.CharField(max_length=64, unique=True, default=_gen_token, db_index=True)

    driver = models.OneToOneField(
        Driver, on_delete=models.CASCADE, related_name='portal_token',
    )

    bound_email = models.EmailField(blank=True)
    email_bound_at = models.DateTimeField(null=True, blank=True)

    is_active = models.BooleanField(default=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'DriverToken({self.driver.full_name})'

    @property
    def is_email_bound(self) -> bool:
        return bool(self.bound_email)

    @property
    def resolved_email(self) -> str:
        return self.bound_email or (self.driver.email or '')

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


class DriverTrustedDevice(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    token = models.ForeignKey(DriverPortalToken, on_delete=models.CASCADE, related_name='trusted_devices')
    device_hash = models.CharField(max_length=64, db_index=True)
    device_label = models.CharField(max_length=255, blank=True)
    last_ip = models.GenericIPAddressField(null=True, blank=True)
    trusted_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    last_seen_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-last_seen_at']
        constraints = [
            models.UniqueConstraint(fields=['token', 'device_hash'], name='uniq_driver_token_device'),
        ]

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


class DriverOTP(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    token = models.ForeignKey(DriverPortalToken, on_delete=models.CASCADE, related_name='otps')
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

    @property
    def is_expired(self) -> bool:
        return self.expires_at and self.expires_at <= timezone.now()

    def consume(self):
        self.consumed_at = timezone.now()
        self.save(update_fields=['consumed_at'])

    @classmethod
    def issue(cls, *, token, device_hash, sent_to, ip=None, device_label=''):
        cls.objects.filter(
            token=token, device_hash=device_hash, consumed_at__isnull=True,
        ).update(consumed_at=timezone.now())
        return cls.objects.create(
            token=token, device_hash=device_hash, sent_to=sent_to,
            requested_ip=ip, device_label=(device_label or '')[:255],
            expires_at=timezone.now() + timedelta(minutes=OTP_TTL_MINUTES),
        )

    @classmethod
    def verify(cls, *, token, device_hash, code) -> tuple[bool, str]:
        otp = (
            cls.objects
            .filter(token=token, device_hash=device_hash, consumed_at__isnull=True)
            .order_by('-created_at').first()
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


# ─────────────────────────────────────────────────────────────────────────────
# Shipment
# ─────────────────────────────────────────────────────────────────────────────

class Shipment(models.Model):
    class Status(models.TextChoices):
        DRAFT = 'draft', _('Draft')
        READY = 'ready', _('Ready for Dispatch')
        DISPATCHED = 'dispatched', _('Dispatched')
        EN_ROUTE = 'en_route', _('En Route')
        ARRIVED = 'arrived', _('Arrived On Site')
        DELIVERED = 'delivered', _('Delivered')
        FAILED = 'failed', _('Delivery Failed')
        CANCELLED = 'cancelled', _('Cancelled')

    OPEN_STATUSES = ['ready', 'dispatched', 'en_route', 'arrived']

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    reference = models.CharField(max_length=30, unique=True, db_index=True)

    status = models.CharField(
        max_length=12, choices=Status.choices, default=Status.DRAFT, db_index=True,
    )

    # ── Link to existing purchase / invoice records ──────────────────────
    # Kept nullable + SET_NULL so a purged order doesn't delete delivery
    # history. Adjust the app labels/models to match your install.
    purchase_order = models.ForeignKey(
        'orders.SalesOrder',
        on_delete=models.SET_NULL, null=True, blank=True,
        related_name='shipments',
        help_text='The sales order this delivery fulfils.',
    )
    invoice = models.ForeignKey(
        'finance.IncomingPaymentRequest',
        on_delete=models.SET_NULL, null=True, blank=True,
        related_name='shipments',
        help_text='Optional linked invoice.',
    )

    # ── Customer / destination ────────────────────────────────────────────
    customer_name = models.CharField(max_length=200)
    contact_name = models.CharField(max_length=160, blank=True)
    contact_phone = models.CharField(max_length=40, blank=True)
    contact_email = models.EmailField(blank=True)

    origin_label = models.CharField(max_length=200, default='Office', help_text='Where it ships from.')
    destination_address = models.TextField(help_text='Customer site address.')
    destination_lat = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    destination_lng = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    plus_code = models.CharField(max_length=32, blank=True, help_text='Google Plus Code, if known.')

    # ── Assignment ─────────────────────────────────────────────────────────
    driver = models.ForeignKey(
        Driver, on_delete=models.SET_NULL, null=True, blank=True, related_name='shipments',
    )
    vehicle = models.ForeignKey(
        Vehicle, on_delete=models.SET_NULL, null=True, blank=True, related_name='shipments',
    )

    scheduled_for = models.DateTimeField(null=True, blank=True)
    dispatched_at = models.DateTimeField(null=True, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)

    instructions = models.TextField(blank=True, help_text='Special handling / delivery notes.')

    created_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, related_name='shipments_created',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['status', '-created_at']),
            models.Index(fields=['driver', 'status']),
        ]

    def __str__(self):
        return f'{self.reference} → {self.customer_name}'

    @property
    def is_open(self) -> bool:
        return self.status in self.OPEN_STATUSES

    @property
    def is_delivered(self) -> bool:
        return self.status == self.Status.DELIVERED

    @property
    def has_destination_coords(self) -> bool:
        return self.destination_lat is not None and self.destination_lng is not None

    @property
    def directions_url(self) -> str:
        """Google Maps directions link the driver can tap."""
        if self.has_destination_coords:
            dest = f'{self.destination_lat},{self.destination_lng}'
        elif self.plus_code:
            dest = self.plus_code
        else:
            dest = self.destination_address.replace('\n', ', ')
        from urllib.parse import quote
        return f'https://www.google.com/maps/dir/?api=1&destination={quote(dest)}'

    @property
    def latest_ping(self):
        return self.location_pings.order_by('-recorded_at').first()

    def add_event(self, status, *, actor=None, note='', by_driver=False):
        return ShipmentEvent.objects.create(
            shipment=self, status=status, actor=actor, note=note, by_driver=by_driver,
        )

    def transition(self, new_status, *, actor=None, note='', by_driver=False):
        """Move to a new status, stamp timestamps, log an event."""
        self.status = new_status
        update = ['status', 'updated_at']
        now = timezone.now()
        if new_status == self.Status.DISPATCHED and not self.dispatched_at:
            self.dispatched_at = now
            update.append('dispatched_at')
            if self.vehicle_id:
                self.vehicle.status = Vehicle.Status.ON_DELIVERY
                self.vehicle.save(update_fields=['status', 'updated_at'])
        if new_status == self.Status.DELIVERED and not self.delivered_at:
            self.delivered_at = now
            update.append('delivered_at')
            if self.vehicle_id and not self.vehicle.is_hired:
                self.vehicle.status = Vehicle.Status.AVAILABLE
                self.vehicle.save(update_fields=['status', 'updated_at'])
        self.save(update_fields=update)
        self.add_event(new_status, actor=actor, note=note, by_driver=by_driver)


class ShipmentItem(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    shipment = models.ForeignKey(Shipment, on_delete=models.CASCADE, related_name='items')
    description = models.CharField(max_length=300)
    quantity = models.DecimalField(max_digits=10, decimal_places=2, default=1)
    unit = models.CharField(max_length=30, blank=True, help_text='e.g. box, unit, pallet')
    position = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ['position', 'id']

    def __str__(self):
        return f'{self.quantity} × {self.description}'


class ShipmentEvent(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    shipment = models.ForeignKey(Shipment, on_delete=models.CASCADE, related_name='events')
    status = models.CharField(max_length=12, choices=Shipment.Status.choices)
    note = models.TextField(blank=True)
    actor = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    by_driver = models.BooleanField(default=False)
    lat = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    lng = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at']

    def __str__(self):
        return f'{self.shipment.reference}: {self.get_status_display()}'


class LocationPing(models.Model):
    """Live GPS breadcrumb posted by the driver portal while en route."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    shipment = models.ForeignKey(Shipment, on_delete=models.CASCADE, related_name='location_pings')
    lat = models.DecimalField(max_digits=9, decimal_places=6)
    lng = models.DecimalField(max_digits=9, decimal_places=6)
    accuracy_m = models.FloatField(null=True, blank=True)
    speed_kph = models.FloatField(null=True, blank=True)
    heading = models.FloatField(null=True, blank=True)
    recorded_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ['-recorded_at']
        indexes = [models.Index(fields=['shipment', '-recorded_at'])]

    def __str__(self):
        return f'{self.shipment.reference} @ {self.lat},{self.lng}'


class ProofOfDelivery(models.Model):
    """Customer sign-off captured at the point of delivery."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    shipment = models.OneToOneField(Shipment, on_delete=models.CASCADE, related_name='proof')

    received_by_name = models.CharField(max_length=160)
    # Drawn signature stored as a data-URL PNG, or typed-name fallback.
    signature_data_url = models.TextField(blank=True)
    signature_typed = models.CharField(max_length=160, blank=True)

    photo = models.ImageField(upload_to='pod_photos/%Y/%m/', null=True, blank=True)
    note = models.TextField(blank=True)

    signed_lat = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    signed_lng = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    signed_at = models.DateTimeField(default=timezone.now)
    signed_ip = models.GenericIPAddressField(null=True, blank=True)

    class Meta:
        ordering = ['-signed_at']

    def __str__(self):
        return f'POD {self.shipment.reference} — {self.received_by_name}'

    @property
    def has_signature(self) -> bool:
        return bool(self.signature_data_url or self.signature_typed)
