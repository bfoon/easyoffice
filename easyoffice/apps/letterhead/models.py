"""
apps/letterhead/models.py

Stores company letterhead configurations.  Only superusers and users
whose profile carries the 'ceo' or 'office_admin' role may create or
update these rows (enforced in views.py via _letterhead_permission).

One row = one saved letterhead design.  The company can keep multiple
designs (e.g. "Main Office", "Legal Department") but only one can be
marked active at a time — that one is used when auto-stamping documents.

v2 additions
────────────
• default_tables  JSONField   — stores an ordered list of table presets
  that appear in the builder's "Insert Table" panel.  Each entry has:

      {
        "id":      "invoice",           # stable slug
        "label":   "Invoice Table",     # shown in UI
        "columns": ["Item","Qty","Rate","Amount"],
        "rows":    2,                   # pre-filled empty rows
        "style":   "striped"            # striped | bordered | minimal
      }

  Users can add, reorder, rename, or delete these presets inside the
  builder; changes are saved back via PATCH /api/templates/<uuid>/update/.
"""
import uuid

from django.conf import settings
from django.db import models


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

# ── Table preset defaults ─────────────────────────────────────────────────────
# Shipped with every new LetterheadTemplate.  Users can customise freely;
# these are only applied when default_tables is None/empty.

DEFAULT_TABLE_PRESETS = [
    {
        "id":      "invoice",
        "label":   "Invoice",
        "columns": ["Description", "Qty", "Unit Price", "Total"],
        "rows":    4,
        "style":   "striped",
    },
    {
        "id":      "quotation",
        "label":   "Quotation",
        "columns": ["Item", "Specification", "Qty", "Rate", "Amount"],
        "rows":    4,
        "style":   "bordered",
    },
    {
        "id":      "attendance",
        "label":   "Attendance",
        "columns": ["Name", "Date", "Time In", "Time Out", "Remarks"],
        "rows":    6,
        "style":   "striped",
    },
    {
        "id":      "minutes",
        "label":   "Meeting Minutes",
        "columns": ["#", "Agenda Item", "Discussion", "Action", "Owner", "Due"],
        "rows":    5,
        "style":   "bordered",
    },
    {
        "id":      "comparison",
        "label":   "Comparison",
        "columns": ["Feature", "Option A", "Option B", "Notes"],
        "rows":    5,
        "style":   "minimal",
    },
    {
        "id":      "contacts",
        "label":   "Contact List",
        "columns": ["Name", "Title", "Email", "Phone"],
        "rows":    5,
        "style":   "minimal",
    },
]

TABLE_STYLE_CHOICES = [
    ('striped',   'Striped'),
    ('bordered',  'Bordered'),
    ('minimal',   'Minimal'),
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

    # ── Default table presets ─────────────────────────────────────────────
    # Stores the user's customised list of insert-table presets.
    # Schema: list of dicts — see DEFAULT_TABLE_PRESETS above.
    # Null = use the system defaults (DEFAULT_TABLE_PRESETS).
    default_tables = models.JSONField(
        null=True,
        blank=True,
        default=None,
        help_text=(
            'Ordered list of table presets available in the builder. '
            'Leave null to use the system defaults.'
        ),
    )

    # ── Body content ──────────────────────────────────────────────────────
    # Rich HTML authored in the builder's editable page body. Persisted so
    # the user's draft is recovered on reload and so the exporters can
    # render it below the letterhead header.
    body_html = models.TextField(
        blank=True, default='',
        help_text='User-edited HTML for the page body (below the header).',
    )

    # ── Audit ─────────────────────────────────────────────────────────────
    created_by = models.ForeignKey(
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
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-is_active', '-updated_at']
        verbose_name = 'Letterhead template'
        verbose_name_plural = 'Letterhead templates'

    def __str__(self):
        flag = ' [active]' if self.is_active else ''
        return f'{self.name}{flag}'

    # ── Table preset helpers ──────────────────────────────────────────────

    def get_tables(self) -> list:
        """
        Return the effective table preset list.
        Falls back to DEFAULT_TABLE_PRESETS when default_tables is unset.
        """
        if self.default_tables:
            return self.default_tables
        return DEFAULT_TABLE_PRESETS

    def reset_tables_to_default(self):
        """Discard any customisations and revert to the system presets."""
        self.default_tables = None
        self.save(update_fields=['default_tables', 'updated_at'])

    def upsert_table_preset(self, preset: dict):
        """
        Add or replace a table preset by its ``id`` field.
        Creates the tables list from defaults first if it doesn't exist yet.
        """
        tables = list(self.get_tables())
        for i, t in enumerate(tables):
            if t.get('id') == preset.get('id'):
                tables[i] = preset
                break
        else:
            tables.append(preset)
        self.default_tables = tables
        self.save(update_fields=['default_tables', 'updated_at'])

    def delete_table_preset(self, preset_id: str):
        """Remove a preset by id.  No-op if not found."""
        tables = [t for t in self.get_tables() if t.get('id') != preset_id]
        self.default_tables = tables
        self.save(update_fields=['default_tables', 'updated_at'])

    def reorder_table_presets(self, ordered_ids: list):
        """
        Re-order presets to match ``ordered_ids``.
        Presets whose id is not in the list are appended at the end.
        """
        current = {t['id']: t for t in self.get_tables()}
        reordered = [current[i] for i in ordered_ids if i in current]
        leftover  = [t for t in self.get_tables() if t['id'] not in ordered_ids]
        self.default_tables = reordered + leftover
        self.save(update_fields=['default_tables', 'updated_at'])

    # ── Existing helpers ──────────────────────────────────────────────────

    def set_active(self):
        """Mark this template as active and deactivate all others."""
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
            'id':              str(self.pk),
            'name':            self.name,
            'is_active':       self.is_active,
            'template_key':    self.template_key,
            'company_name':    self.company_name,
            'tagline':         self.tagline,
            'address':         self.address,
            'contact_info':    self.contact_info,
            'logo_url':        self.logo.url if self.logo else None,
            'color_primary':   self.color_primary,
            'color_secondary': self.color_secondary,
            'font_header':     self.font_header,
            'font_body':       self.font_body,
            # Always include the resolved table presets so the JS builder
            # never has to guess whether to use defaults or custom data.
            'default_tables':  self.get_tables(),
            # User-edited body content (HTML). Empty string if never edited.
            'body_html':       self.body_html or '',
        }

    @classmethod
    def get_active(cls):
        """Return the currently active template, or None."""
        return cls.objects.filter(is_active=True).first()