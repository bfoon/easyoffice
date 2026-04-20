import uuid
import secrets
import string
from datetime import timedelta

from django.db import models
from django.contrib.auth.models import AbstractUser
from django.utils.translation import gettext_lazy as _
from django.utils import timezone


def _generate_otp(length=6):
    return ''.join(secrets.choice(string.digits) for _ in range(length))

def _generate_token(length=48):
    return secrets.token_urlsafe(length)


class User(AbstractUser):
    class Status(models.TextChoices):
        ACTIVE     = 'active',     _('Active')
        INACTIVE   = 'inactive',   _('Inactive')
        ON_LEAVE   = 'on_leave',   _('On Leave')
        SUSPENDED  = 'suspended',  _('Suspended')
        TERMINATED = 'terminated', _('Terminated')

    class Theme(models.TextChoices):
        LIGHT = 'light', _('Light')
        DARK  = 'dark',  _('Dark')
        AUTO  = 'auto',  _('Auto')

    id                  = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    email               = models.EmailField(unique=True)
    avatar              = models.ImageField(upload_to='avatars/', null=True, blank=True)
    phone               = models.CharField(max_length=30, blank=True)
    status              = models.CharField(max_length=20, choices=Status.choices, default=Status.ACTIVE)
    employee_id         = models.CharField(max_length=50, unique=True, null=True, blank=True)
    date_joined_office  = models.DateField(null=True, blank=True)
    last_seen           = models.DateTimeField(null=True, blank=True)
    is_online           = models.BooleanField(default=False)
    theme               = models.CharField(max_length=10, choices=Theme.choices, default=Theme.LIGHT)
    notification_email  = models.BooleanField(default=True)
    notification_push   = models.BooleanField(default=True)
    compact_view        = models.BooleanField(default=False)
    timezone_pref       = models.CharField(max_length=50, default='UTC')
    language            = models.CharField(max_length=10, default='en')
    bio                 = models.TextField(blank=True)
    skills              = models.TextField(blank=True)
    linkedin_url        = models.URLField(blank=True)

    # Security
    active_device       = models.ForeignKey(
        'TrustedDevice', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='active_sessions',
    )
    failed_login_count  = models.PositiveSmallIntegerField(default=0)
    lockout_until       = models.DateTimeField(null=True, blank=True)
    last_login_ip       = models.GenericIPAddressField(null=True, blank=True)
    last_login_lat      = models.FloatField(null=True, blank=True)
    last_login_lng      = models.FloatField(null=True, blank=True)
    last_login_city     = models.CharField(max_length=100, blank=True)
    last_login_country  = models.CharField(max_length=100, blank=True)

    USERNAME_FIELD  = 'email'
    REQUIRED_FIELDS = ['username', 'first_name', 'last_name']

    class Meta:
        verbose_name        = _('User')
        verbose_name_plural = _('Users')
        ordering            = ['last_name', 'first_name']

    def __str__(self):
        return self.get_full_name() or self.email

    @property
    def full_name(self):
        return f'{self.first_name} {self.last_name}'.strip() or self.email

    @property
    def initials(self):
        parts = self.full_name.split()
        return ''.join(p[0].upper() for p in parts[:2]) if parts else 'U'

    @property
    def staff_profile(self):
        try:
            return self.staffprofile
        except Exception:
            return None

    def update_last_seen(self):
        self.last_seen = timezone.now()
        self.is_online = True
        self.save(update_fields=['last_seen', 'is_online'])

    def is_locked_out(self):
        return bool(self.lockout_until and self.lockout_until > timezone.now())

    def record_failed_login(self, max_attempts, lockout_minutes):
        self.failed_login_count += 1
        if self.failed_login_count >= max_attempts:
            self.lockout_until = timezone.now() + timedelta(minutes=lockout_minutes)
        self.save(update_fields=['failed_login_count', 'lockout_until'])
        return self.is_locked_out()

    def clear_failed_logins(self):
        if self.failed_login_count or self.lockout_until:
            self.failed_login_count = 0
            self.lockout_until = None
            self.save(update_fields=['failed_login_count', 'lockout_until'])


class TrustedDevice(models.Model):
    TRUST_DAYS = 30

    id          = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user        = models.ForeignKey(User, on_delete=models.CASCADE, related_name='trusted_devices')
    device_id   = models.CharField(max_length=64)
    device_name = models.CharField(max_length=200, blank=True)
    user_agent  = models.TextField(blank=True)
    ip_address  = models.GenericIPAddressField(null=True, blank=True)
    city        = models.CharField(max_length=100, blank=True)
    country     = models.CharField(max_length=100, blank=True)
    trusted_at  = models.DateTimeField(auto_now_add=True)
    expires_at  = models.DateTimeField()
    last_used   = models.DateTimeField(null=True, blank=True)
    is_revoked  = models.BooleanField(default=False)

    class Meta:
        unique_together = [('user', 'device_id')]
        ordering        = ['-trusted_at']

    def __str__(self):
        return f'{self.user.email} — {self.device_name or self.device_id[:12]}'

    def is_valid(self):
        return not self.is_revoked and self.expires_at > timezone.now()

    def touch(self):
        self.last_used = timezone.now()
        self.save(update_fields=['last_used'])

    @classmethod
    def create_for(cls, user, device_id, device_name='', user_agent='',
                   ip_address=None, city='', country=''):
        obj, _ = cls.objects.update_or_create(
            user=user, device_id=device_id,
            defaults=dict(
                device_name=device_name, user_agent=user_agent,
                ip_address=ip_address, city=city, country=country,
                expires_at=timezone.now() + timedelta(days=cls.TRUST_DAYS),
                is_revoked=False,
            )
        )
        return obj


class OTPChallenge(models.Model):
    class Purpose(models.TextChoices):
        DEVICE_VERIFY  = 'device_verify',  _('New Device Verification')
        SWITCH_CONFIRM = 'switch_confirm', _('Device Switch Confirmation')
        UNTRUSTED_WARN = 'untrusted_warn', _('Untrusted Device Warning')

    EXPIRY_MINUTES = 10
    MAX_ATTEMPTS   = 5

    id                  = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user                = models.ForeignKey(User, on_delete=models.CASCADE, related_name='otp_challenges')
    purpose             = models.CharField(max_length=20, choices=Purpose.choices, default=Purpose.DEVICE_VERIFY)
    code                = models.CharField(max_length=10)
    pending_device_id   = models.CharField(max_length=64, blank=True)
    pending_device_name = models.CharField(max_length=200, blank=True)
    pending_ip          = models.GenericIPAddressField(null=True, blank=True)
    pending_city        = models.CharField(max_length=100, blank=True)
    pending_country     = models.CharField(max_length=100, blank=True)
    pending_user_agent  = models.TextField(blank=True)
    attempts            = models.PositiveSmallIntegerField(default=0)
    is_used             = models.BooleanField(default=False)
    created_at          = models.DateTimeField(auto_now_add=True)
    expires_at          = models.DateTimeField()

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'OTP({self.purpose}) for {self.user.email}'

    def is_valid(self):
        return (not self.is_used
                and self.expires_at > timezone.now()
                and self.attempts < self.MAX_ATTEMPTS)

    def verify(self, entered_code):
        if not self.is_valid():
            return False
        self.attempts += 1
        if self.code == entered_code.strip():
            self.is_used = True
            self.save(update_fields=['attempts', 'is_used'])
            return True
        self.save(update_fields=['attempts'])
        return False

    @classmethod
    def issue(cls, user, purpose, pending_device_id='', pending_device_name='',
              pending_ip=None, pending_city='', pending_country='', pending_user_agent=''):
        cls.objects.filter(user=user, purpose=purpose, is_used=False).update(is_used=True)
        return cls.objects.create(
            user=user, purpose=purpose,
            code=_generate_otp(),
            pending_device_id=pending_device_id,
            pending_device_name=pending_device_name,
            pending_ip=pending_ip,
            pending_city=pending_city,
            pending_country=pending_country,
            pending_user_agent=pending_user_agent,
            expires_at=timezone.now() + timedelta(minutes=cls.EXPIRY_MINUTES),
        )


class DeviceSwitchToken(models.Model):
    EXPIRY_MINUTES = 15

    id              = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user            = models.ForeignKey(User, on_delete=models.CASCADE, related_name='switch_tokens')
    token           = models.CharField(max_length=80, unique=True, default=_generate_token)
    new_device_id   = models.CharField(max_length=64)
    new_device_name = models.CharField(max_length=200, blank=True)
    new_ip          = models.GenericIPAddressField(null=True, blank=True)
    new_city        = models.CharField(max_length=100, blank=True)
    new_country     = models.CharField(max_length=100, blank=True)
    new_user_agent  = models.TextField(blank=True)
    is_used         = models.BooleanField(default=False)
    created_at      = models.DateTimeField(auto_now_add=True)
    expires_at      = models.DateTimeField()

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'SwitchToken for {self.user.email}'

    def is_valid(self):
        return not self.is_used and self.expires_at > timezone.now()

    @classmethod
    def issue(cls, user, new_device_id, new_device_name='', new_ip=None,
              new_city='', new_country='', new_user_agent=''):
        cls.objects.filter(user=user, is_used=False).update(is_used=True)
        return cls.objects.create(
            user=user,
            new_device_id=new_device_id,
            new_device_name=new_device_name,
            new_ip=new_ip,
            new_city=new_city,
            new_country=new_country,
            new_user_agent=new_user_agent,
            expires_at=timezone.now() + timedelta(minutes=cls.EXPIRY_MINUTES),
        )


class SecuritySettings(models.Model):
    """Singleton — only one row. Edit via Django admin."""
    lockout_enabled             = models.BooleanField(default=True)
    max_failed_attempts         = models.PositiveSmallIntegerField(default=5)
    lockout_duration_minutes    = models.PositiveSmallIntegerField(default=30)
    geo_alert_enabled           = models.BooleanField(default=True)
    geo_alert_distance_km       = models.PositiveIntegerField(default=500)
    geo_alert_minutes           = models.PositiveSmallIntegerField(default=60)
    otp_expiry_minutes          = models.PositiveSmallIntegerField(default=10)
    otp_max_attempts            = models.PositiveSmallIntegerField(default=5)
    device_trust_days           = models.PositiveSmallIntegerField(default=30)

    class Meta:
        verbose_name        = _('Security Settings')
        verbose_name_plural = _('Security Settings')

    def __str__(self):
        return 'Security Settings'

    @classmethod
    def get(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class SecurityEvent(models.Model):
    class EventType(models.TextChoices):
        LOGIN_SUCCESS     = 'login_success',     _('Login — Success')
        LOGIN_FAILED      = 'login_failed',       _('Login — Failed')
        ACCOUNT_LOCKED    = 'account_locked',     _('Account Locked')
        OTP_SENT          = 'otp_sent',           _('OTP Sent')
        OTP_SUCCESS       = 'otp_success',        _('OTP — Verified')
        OTP_FAILED        = 'otp_failed',         _('OTP — Wrong Code')
        OTP_EXPIRED       = 'otp_expired',        _('OTP — Expired')
        DEVICE_TRUSTED    = 'device_trusted',     _('Device Trusted')
        DEVICE_REVOKED    = 'device_revoked',     _('Device Revoked')
        SWITCH_REQUESTED  = 'switch_requested',   _('Device Switch Requested')
        SWITCH_APPROVED   = 'switch_approved',    _('Device Switch Approved')
        UNTRUSTED_ATTEMPT = 'untrusted_attempt',  _('Untrusted Device Attempt')
        GEO_ALERT         = 'geo_alert',          _('Geo-velocity Alert')
        LOGOUT            = 'logout',             _('Logout')

    id          = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user        = models.ForeignKey(User, on_delete=models.CASCADE, related_name='security_events')
    event_type  = models.CharField(max_length=30, choices=EventType.choices)
    ip_address  = models.GenericIPAddressField(null=True, blank=True)
    device_id   = models.CharField(max_length=64, blank=True)
    device_name = models.CharField(max_length=200, blank=True)
    city        = models.CharField(max_length=100, blank=True)
    country     = models.CharField(max_length=100, blank=True)
    detail      = models.TextField(blank=True)
    created_at  = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.event_type} — {self.user.email} @ {self.created_at:%Y-%m-%d %H:%M}'

    @classmethod
    def log(cls, user, event_type, ip_address=None, device_id='',
            device_name='', city='', country='', detail=''):
        return cls.objects.create(
            user=user, event_type=event_type, ip_address=ip_address,
            device_id=device_id, device_name=device_name,
            city=city, country=country, detail=detail,
        )


# ── Original models (unchanged) ───────────────────────────────────────────────

class OfficeSettings(models.Model):
    key        = models.CharField(max_length=100, unique=True)
    value      = models.TextField()
    value_type = models.CharField(
        max_length=20, default='string',
        choices=[('string','String'),('integer','Integer'),
                 ('boolean','Boolean'),('json','JSON'),('color','Color')],
    )
    label       = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    group       = models.CharField(max_length=50, default='general')
    is_public   = models.BooleanField(default=False)
    updated_at  = models.DateTimeField(auto_now=True)
    updated_by  = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)

    class Meta:
        ordering = ['group', 'key']

    def __str__(self):
        return f'{self.group}/{self.key}'

    @classmethod
    def get(cls, key, default=None):
        try:
            s = cls.objects.get(key=key)
            if s.value_type == 'boolean':
                return s.value.lower() in ('true', '1', 'yes')
            elif s.value_type == 'integer':
                return int(s.value)
            return s.value
        except cls.DoesNotExist:
            return default


class AuditLog(models.Model):
    class Action(models.TextChoices):
        CREATE  = 'create',  _('Create')
        UPDATE  = 'update',  _('Update')
        DELETE  = 'delete',  _('Delete')
        LOGIN   = 'login',   _('Login')
        LOGOUT  = 'logout',  _('Logout')
        EXPORT  = 'export',  _('Export')
        APPROVE = 'approve', _('Approve')
        REJECT  = 'reject',  _('Reject')

    id          = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user        = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name='audit_logs')
    action      = models.CharField(max_length=20, choices=Action.choices)
    model_name  = models.CharField(max_length=100)
    object_id   = models.CharField(max_length=100, blank=True)
    object_repr = models.CharField(max_length=255, blank=True)
    changes     = models.JSONField(null=True, blank=True)
    ip_address  = models.GenericIPAddressField(null=True, blank=True)
    timestamp   = models.DateTimeField(auto_now_add=True)
    notes       = models.TextField(blank=True)

    class Meta:
        ordering = ['-timestamp']


class CoreNotification(models.Model):
    class Type(models.TextChoices):
        TASK      = 'task',      _('Task')
        PROJECT   = 'project',   _('Project')
        MESSAGE   = 'message',   _('Message')
        APPRAISAL = 'appraisal', _('Appraisal')
        LEAVE     = 'leave',     _('Leave')
        FINANCE   = 'finance',   _('Finance')
        IT        = 'it',        _('IT')
        SECURITY  = 'security',  _('Security')
        SYSTEM    = 'system',    _('System')

    id                = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    recipient         = models.ForeignKey(User, on_delete=models.CASCADE, related_name='core_notifications')
    sender            = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True,
                                          related_name='sent_notifications')
    notification_type = models.CharField(max_length=20, choices=Type.choices)
    title             = models.CharField(max_length=255)
    message           = models.TextField()
    link              = models.CharField(max_length=500, blank=True)
    is_read           = models.BooleanField(default=False)
    read_at           = models.DateTimeField(null=True, blank=True)
    created_at        = models.DateTimeField(auto_now_add=True)
    data              = models.JSONField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']

    def mark_read(self):
        if not self.is_read:
            self.is_read = True
            self.read_at = timezone.now()
            self.save(update_fields=['is_read', 'read_at'])