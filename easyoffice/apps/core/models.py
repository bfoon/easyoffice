import uuid
from django.db import models
from django.contrib.auth.models import AbstractUser
from django.utils.translation import gettext_lazy as _
from django.utils import timezone


class User(AbstractUser):
    class Status(models.TextChoices):
        ACTIVE = 'active', _('Active')
        INACTIVE = 'inactive', _('Inactive')
        ON_LEAVE = 'on_leave', _('On Leave')
        SUSPENDED = 'suspended', _('Suspended')
        TERMINATED = 'terminated', _('Terminated')

    class Theme(models.TextChoices):
        LIGHT = 'light', _('Light')
        DARK = 'dark', _('Dark')
        AUTO = 'auto', _('Auto')

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    email = models.EmailField(unique=True)
    avatar = models.ImageField(upload_to='avatars/', null=True, blank=True)
    phone = models.CharField(max_length=30, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ACTIVE)
    employee_id = models.CharField(max_length=50, unique=True, null=True, blank=True)
    date_joined_office = models.DateField(null=True, blank=True)
    last_seen = models.DateTimeField(null=True, blank=True)
    is_online = models.BooleanField(default=False)
    theme = models.CharField(max_length=10, choices=Theme.choices, default=Theme.LIGHT)
    notification_email = models.BooleanField(default=True)
    notification_push = models.BooleanField(default=True)
    compact_view = models.BooleanField(default=False)
    timezone_pref = models.CharField(max_length=50, default='UTC')
    language = models.CharField(max_length=10, default='en')
    bio = models.TextField(blank=True)
    skills = models.TextField(blank=True)
    linkedin_url = models.URLField(blank=True)

    USERNAME_FIELD = 'email'
    REQUIRED_FIELDS = ['username', 'first_name', 'last_name']

    class Meta:
        verbose_name = _('User')
        verbose_name_plural = _('Users')
        ordering = ['last_name', 'first_name']

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


class OfficeSettings(models.Model):
    key = models.CharField(max_length=100, unique=True)
    value = models.TextField()
    value_type = models.CharField(max_length=20, default='string',
                                  choices=[('string','String'),('integer','Integer'),
                                           ('boolean','Boolean'),('json','JSON'),('color','Color')])
    label = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    group = models.CharField(max_length=50, default='general')
    is_public = models.BooleanField(default=False)
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)

    class Meta:
        ordering = ['group', 'key']

    def __str__(self):
        return f'{self.group}/{self.key}'

    @classmethod
    def get(cls, key, default=None):
        try:
            s = cls.objects.get(key=key)
            if s.value_type == 'boolean':
                return s.value.lower() in ('true','1','yes')
            elif s.value_type == 'integer':
                return int(s.value)
            return s.value
        except cls.DoesNotExist:
            return default


class AuditLog(models.Model):
    class Action(models.TextChoices):
        CREATE = 'create', _('Create')
        UPDATE = 'update', _('Update')
        DELETE = 'delete', _('Delete')
        LOGIN = 'login', _('Login')
        LOGOUT = 'logout', _('Logout')
        EXPORT = 'export', _('Export')
        APPROVE = 'approve', _('Approve')
        REJECT = 'reject', _('Reject')

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name='audit_logs')
    action = models.CharField(max_length=20, choices=Action.choices)
    model_name = models.CharField(max_length=100)
    object_id = models.CharField(max_length=100, blank=True)
    object_repr = models.CharField(max_length=255, blank=True)
    changes = models.JSONField(null=True, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    timestamp = models.DateTimeField(auto_now_add=True)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ['-timestamp']


class CoreNotification(models.Model):
    class Type(models.TextChoices):
        TASK = 'task', _('Task')
        PROJECT = 'project', _('Project')
        MESSAGE = 'message', _('Message')
        APPRAISAL = 'appraisal', _('Appraisal')
        LEAVE = 'leave', _('Leave')
        FINANCE = 'finance', _('Finance')
        IT = 'it', _('IT')
        SYSTEM = 'system', _('System')

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    recipient = models.ForeignKey(User, on_delete=models.CASCADE, related_name='core_notifications')
    sender = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='sent_notifications')
    notification_type = models.CharField(max_length=20, choices=Type.choices)
    title = models.CharField(max_length=255)
    message = models.TextField()
    link = models.CharField(max_length=500, blank=True)
    is_read = models.BooleanField(default=False)
    read_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    data = models.JSONField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']

    def mark_read(self):
        if not self.is_read:
            self.is_read = True
            self.read_at = timezone.now()
            self.save(update_fields=['is_read', 'read_at'])
