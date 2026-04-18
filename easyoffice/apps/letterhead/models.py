"""
apps/letterhead/models.py

Stores company letterhead configurations.  Only superusers and users
whose profile carries the 'ceo' or 'office_admin' role may create or
update these rows (enforced in views.py via _letterhead_permission).

One row = one saved letterhead design.  The company can keep multiple
designs (e.g. "Main Office", "Legal Department") but only one can be
marked active at a time — that one is used when auto-stamping documents.
"""
import uuid
from django.db import models
from django.conf import settings
from django.utils import timezone


# ── Choices ──────────────────────────────────────────────────────────────────

TEMPLATE_CHOICES = [
    ('classic',  'Classic'),
    ('modern',   'Modern'),
    ('bold',     'Bold'),
    ('minimal',  'Minimal'),
    ('split',    'Split'),
    ('elegant',  'Elegant'),
]

FONT_CHOICES = [
    ('Georgia',           'Georgia'),
    ("'Times New Roman'", 'Times New Roman'),
    ('Palatino',          'Palatino'),
    ('Arial',             'Arial'),
    ('Helvetica',         'Helvetica'),
    ('Verdana',           'Verdana'),
    ('Trebuchet MS',      'Trebuchet MS'),
    ("'Courier New'",     'Courier New'),
]


# ── Model ────────────────────────────────────────────────────────────────────

class LetterheadTemplate(models.Model):
    """
    A single letterhead configuration.

    Fields map 1-to-1 to the builder UI so the front-end can rehydrate a
    saved design exactly by reading this row as JSON.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # Human-readable label shown in the management list.
    name = models.CharField(max_length=120)

    # Only one template can be the active / default at any time.
    # Enforced by LetterheadTemplate.set_active().
    is_active = models.BooleanField(default=False, db_index=True)

    # ── Visual template ──────────────────────────────────────────────────
    template_key = models.CharField(
        max_length=20,
        choices=TEMPLATE_CHOICES,
        default='classic',
    )

    # ── Company info ─────────────────────────────────────────────────────
    company_name = models.CharField(max_length=200)
    tagline      = models.CharField(max_length=200, blank=True)
    address      = models.TextField(blank=True)
    contact_info = models.CharField(max_length=300, blank=True,
                                    help_text='Phone, email, website — one line')

    # ── Branding ─────────────────────────────────────────────────────────
    # Logo stored in MEDIA_ROOT/letterhead/logos/
    logo = models.ImageField(
        upload_to='letterhead/logos/',
        null=True, blank=True,
        help_text='PNG / SVG / JPG.  Displayed at ~48 px height.',
    )

    color_primary   = models.CharField(max_length=7, default='#1D9E75',
                                        help_text='Hex colour, e.g. #1D9E75')
    color_secondary = models.CharField(max_length=7, default='#2C2C2A')

    font_header = models.CharField(max_length=40, choices=FONT_CHOICES, default='Georgia')
    font_body   = models.CharField(max_length=40, choices=FONT_CHOICES, default='Arial')

    # ── Audit ─────────────────────────────────────────────────────────────
    created_by  = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='letterhead_templates_created',
    )
    last_edited_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='letterhead_templates_edited',
    )
    created_at  = models.DateTimeField(auto_now_add=True)
    updated_at  = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-is_active', '-updated_at']
        verbose_name = 'Letterhead template'
        verbose_name_plural = 'Letterhead templates'

    def __str__(self):
        flag = ' [active]' if self.is_active else ''
        return f'{self.name}{flag}'

    # ── Helpers ───────────────────────────────────────────────────────────

    def set_active(self):
        """
        Mark this template as the active one and deactivate all others.
        Uses a single UPDATE to avoid a race between two admins.
        """
        LetterheadTemplate.objects.exclude(pk=self.pk).filter(
            is_active=True
        ).update(is_active=False)
        self.is_active = True
        self.save(update_fields=['is_active', 'updated_at'])

    def to_builder_dict(self):
        """
        Serialise to the flat dict the JS builder expects.
        Consumed by LetterheadBuilderView and the REST endpoint.
        """
        return {
            'id':             str(self.pk),
            'name':           self.name,
            'is_active':      self.is_active,
            'template_key':   self.template_key,
            'company_name':   self.company_name,
            'tagline':        self.tagline,
            'address':        self.address,
            'contact_info':   self.contact_info,
            'logo_url':       self.logo.url if self.logo else None,
            'color_primary':  self.color_primary,
            'color_secondary': self.color_secondary,
            'font_header':    self.font_header,
            'font_body':      self.font_body,
        }

    @classmethod
    def get_active(cls):
        """Return the currently active template, or None."""
        return cls.objects.filter(is_active=True).first()
