import uuid

from django.conf import settings
from django.db import models


class FeatureFlag(models.Model):
    """
    Runtime state for one FeatureSpec. A missing row means "use the spec's
    default" — rows are only written when a superuser changes something.
    """

    class Mode(models.TextChoices):
        ON    = 'on',    'On for everyone'
        PILOT = 'pilot', 'Pilot (selected people only)'
        OFF   = 'off',   'Off'

    id   = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    key  = models.CharField(max_length=60, unique=True)
    mode = models.CharField(max_length=10, choices=Mode.choices, default=Mode.ON)

    # Pilot audience
    pilot_users  = models.ManyToManyField(settings.AUTH_USER_MODEL, blank=True,
                                          related_name='pilot_features')
    pilot_groups = models.ManyToManyField('auth.Group', blank=True,
                                          related_name='pilot_features')

    superuser_preview = models.BooleanField(
        default=False,
        help_text='Superusers can still open the module while it is off (with a banner).',
    )
    off_message = models.CharField(
        max_length=300, blank=True,
        help_text='Shown to people who follow a link into the module while it is off.',
    )

    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                   null=True, blank=True, related_name='+')
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['key']

    def __str__(self):
        return f'{self.key}: {self.mode}'


class FeatureAuditLog(models.Model):
    id         = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    key        = models.CharField(max_length=60, db_index=True)
    old_mode   = models.CharField(max_length=10, blank=True)
    new_mode   = models.CharField(max_length=10, blank=True)
    note       = models.CharField(max_length=400, blank=True)
    actor      = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                   null=True, blank=True, related_name='+')
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.key} {self.old_mode}→{self.new_mode}'
