"""
apps/customforms/models.py

Custom Forms with configurable approval flows.

Concepts
────────
FormTemplate      — a designed form: an ordered list of fields (schema JSON)
                    plus an ordered approval flow (approval_flow JSON) plus
                    an optional letterhead attachment.
FormSubmission    — one filled-in instance of a template, with a frozen copy
                    of the schema/flow at submission time (so later edits to
                    the template never corrupt in-flight approvals).
SubmissionStep    — a resolved approval step on a submission.  Sequential:
                    step N+1 only becomes 'pending' after step N is approved.

Field schema (FormTemplate.schema — list of dicts)
──────────────────────────────────────────────────
  {
    "id":        "f_ab12cd",              # stable slug, generated client-side
    "type":      "text",                  # see FIELD_TYPES below
    "label":     "Employee Name",
    "help":      "",                      # small helper text under the field
    "required":  true,
    "width":     "full",                  # full | half | third
    "placeholder": "",
    "options":   ["A", "B"],              # select / radio / checkboxes
    "columns":   ["Item", "Qty"],         # table type
    "rows":      3,                       # table: starting empty rows
  }

FIELD_TYPES the builder ships with:
  heading, paragraph, divider              (layout, no data)
  text, textarea, number, date, time,
  email, phone, select, radio,
  checkboxes, yesno, table, currency

Approval flow schema (FormTemplate.approval_flow — list of dicts)
─────────────────────────────────────────────────────────────────
  {
    "id":            "s_9f31",
    "label":         "HR Review",
    "action":        "approve",           # approve | sign  (sign = approve + drawn signature)
    "assignee_type": "role",              # user | role | position | unit | submitter_choice
    "assignee_value": "office_admin",     # user id / role slug / position / unit name;
                                          # empty when submitter_choice
  }

'submitter_choice' steps are resolved at submission time: the fill page shows
a user picker and the chosen user is stored on the SubmissionStep.
"""
import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone


# ── Choices ──────────────────────────────────────────────────────────────────

TEMPLATE_STATUS = [
    ('draft',     'Draft'),
    ('published', 'Published'),
    ('archived',  'Archived'),
]

LETTERHEAD_MODE = [
    ('none',     'No letterhead (simple)'),
    ('active',   'Company default (active letterhead)'),
    ('specific', 'A specific letterhead'),
]

SUBMISSION_STATUS = [
    ('in_review', 'In review'),
    ('approved',  'Approved'),
    ('rejected',  'Rejected'),
    ('cancelled', 'Cancelled'),
]

STEP_STATUS = [
    ('waiting',  'Waiting'),    # earlier steps not finished yet
    ('pending',  'Pending'),    # it is this step's turn
    ('approved', 'Approved'),
    ('rejected', 'Rejected'),
    ('skipped',  'Skipped'),
]

STEP_ACTIONS = [
    ('approve', 'Approve'),
    ('sign',    'Approve + signature'),
]

ASSIGNEE_TYPES = [
    ('user',             'Specific user'),
    ('role',             'Anyone with role'),
    ('position',         'Anyone with position'),
    ('unit',             'Anyone in unit'),
    ('submitter_choice', 'Submitter chooses at submission'),
]


# ── FormTemplate ─────────────────────────────────────────────────────────────

class FormTemplate(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    title       = models.CharField(max_length=200)
    description = models.TextField(blank=True, default='')
    status      = models.CharField(max_length=12, choices=TEMPLATE_STATUS,
                                   default='draft', db_index=True)

    # Ordered list of field definitions — see module docstring.
    schema = models.JSONField(default=list, blank=True)

    # Ordered list of approval step definitions — see module docstring.
    # Empty list = no approval needed; submissions auto-approve.
    approval_flow = models.JSONField(default=list, blank=True)

    # ── Letterhead attachment ────────────────────────────────────────────
    letterhead_mode = models.CharField(max_length=10, choices=LETTERHEAD_MODE,
                                       default='none')
    letterhead = models.ForeignKey(
        'letterhead.LetterheadTemplate',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='form_templates',
        help_text='Used only when letterhead_mode = specific.',
    )

    # Who may submit: everyone logged in, or restrict later if needed.
    is_anonymous_ref = models.BooleanField(
        default=False,
        help_text='Reserved for future public-link submissions.',
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='form_templates_created')
    last_edited_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='form_templates_edited')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-updated_at']
        verbose_name = 'Form template'

    def __str__(self):
        return f'{self.title} [{self.status}]'

    # ── Letterhead resolution ────────────────────────────────────────────

    def resolve_letterhead(self):
        """Return the LetterheadTemplate to stamp on exports, or None."""
        if self.letterhead_mode == 'none':
            return None
        if self.letterhead_mode == 'specific':
            return self.letterhead
        # 'active' → the company default
        try:
            from apps.letterhead.models import LetterheadTemplate
            return LetterheadTemplate.get_active()
        except Exception:
            return None

    # ── Serialisation for the JS builder ─────────────────────────────────

    def to_builder_dict(self):
        return {
            'id':              str(self.pk),
            'title':           self.title,
            'description':     self.description,
            'status':          self.status,
            'schema':          self.schema or [],
            'approval_flow':   self.approval_flow or [],
            'letterhead_mode': self.letterhead_mode,
            'letterhead_id':   str(self.letterhead_id) if self.letterhead_id else None,
            'updated_at':      self.updated_at.isoformat() if self.updated_at else None,
        }


# ── FormSubmission ───────────────────────────────────────────────────────────

class FormSubmission(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    template = models.ForeignKey(FormTemplate, on_delete=models.PROTECT,
                                 related_name='submissions')

    # Human-friendly reference number, e.g. FRM-2026-0042
    ref_no = models.CharField(max_length=20, unique=True, editable=False)

    # Frozen copies taken at submit time — template edits never break these.
    schema_snapshot = models.JSONField(default=list)
    data            = models.JSONField(default=dict)   # {field_id: value}

    status = models.CharField(max_length=12, choices=SUBMISSION_STATUS,
                              default='in_review', db_index=True)

    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name='form_submissions')
    submitted_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-submitted_at']

    def __str__(self):
        return f'{self.ref_no} — {self.template.title}'

    # ── Reference number ─────────────────────────────────────────────────

    @classmethod
    def next_ref_no(cls):
        year = timezone.now().year
        prefix = f'FRM-{year}-'
        count = cls.objects.filter(ref_no__startswith=prefix).count() + 1
        return f'{prefix}{count:04d}'

    def save(self, *args, **kwargs):
        if not self.ref_no:
            self.ref_no = self.next_ref_no()
        super().save(*args, **kwargs)

    # ── Flow helpers ─────────────────────────────────────────────────────

    @property
    def current_step(self):
        return self.steps.filter(status='pending').order_by('order').first()

    def advance(self):
        """
        Called after a step is approved.  Promotes the next waiting step to
        pending, or completes the submission if none remain.
        """
        nxt = self.steps.filter(status='waiting').order_by('order').first()
        if nxt:
            nxt.status = 'pending'
            nxt.save(update_fields=['status'])
        else:
            self.status = 'approved'
            self.completed_at = timezone.now()
            self.save(update_fields=['status', 'completed_at'])

    def reject(self):
        self.status = 'rejected'
        self.completed_at = timezone.now()
        self.save(update_fields=['status', 'completed_at'])
        # Later steps will never run.
        self.steps.filter(status='waiting').update(status='skipped')


# ── SubmissionStep ───────────────────────────────────────────────────────────

class SubmissionStep(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    submission = models.ForeignKey(FormSubmission, on_delete=models.CASCADE,
                                   related_name='steps')
    order  = models.PositiveIntegerField()
    label  = models.CharField(max_length=120)
    action = models.CharField(max_length=10, choices=STEP_ACTIONS, default='approve')

    assignee_type  = models.CharField(max_length=20, choices=ASSIGNEE_TYPES)
    assignee_value = models.CharField(max_length=200, blank=True, default='')

    # Resolved concrete user (always set for 'user' and 'submitter_choice';
    # set on group-type steps at the moment somebody acts).
    assigned_user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='form_steps_assigned')

    status = models.CharField(max_length=10, choices=STEP_STATUS,
                              default='waiting', db_index=True)

    acted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='form_steps_acted')
    acted_at = models.DateTimeField(null=True, blank=True)
    comment  = models.TextField(blank=True, default='')

    # Drawn signature captured on 'sign' steps — PNG data URL.
    signature_data = models.TextField(blank=True, default='')

    class Meta:
        ordering = ['order']
        unique_together = [('submission', 'order')]

    def __str__(self):
        return f'{self.submission.ref_no} · step {self.order}: {self.label} [{self.status}]'

    # ── Who may act on this step? ────────────────────────────────────────

    def user_can_act(self, user) -> bool:
        if self.status != 'pending' or not user.is_authenticated:
            return False
        if self.assigned_user_id:
            return user.pk == self.assigned_user_id
        # Group-type step: match against staff profile attributes.
        return profile_matches(user, self.assignee_type, self.assignee_value)

    def assignee_display(self) -> str:
        if self.assigned_user_id:
            u = self.assigned_user
            full = (u.get_full_name() or u.get_username()) if u else '—'
            return full
        labels = {
            'role':     f'Role: {self.assignee_value}',
            'position': f'Position: {self.assignee_value}',
            'unit':     f'Unit: {self.assignee_value}',
            'submitter_choice': 'Chosen at submission',
        }
        return labels.get(self.assignee_type, self.assignee_value or '—')


# ── Profile matching (adjust field names here if yours differ) ───────────────
#
# Your staff profile is reached via user.staffprofile (as used by the
# letterhead permission check).  The attribute names below are tried in
# order for each concept — edit PROFILE_ATTRS once if your model differs
# and the whole app follows.

PROFILE_ATTRS = {
    'role':     ['role'],
    'position': ['position', 'job_title', 'title'],
    'unit':     ['unit', 'department', 'dept'],
}


def _get_profile(user):
    return getattr(user, 'staffprofile', None)


def profile_matches(user, concept: str, value: str) -> bool:
    """True if the user's staff profile has `concept` equal to `value`."""
    if not value:
        return False
    profile = _get_profile(user)
    if not profile:
        return False
    for attr in PROFILE_ATTRS.get(concept, []):
        v = getattr(profile, attr, None)
        if v is None:
            continue
        # Handle FK-style unit/position objects gracefully.
        v = getattr(v, 'name', v)
        if str(v).strip().lower() == str(value).strip().lower():
            return True
    return False


def distinct_profile_values(concept: str) -> list:
    """
    Collect distinct role/position/unit values across all staff profiles
    for the builder's assignee dropdowns.  Fails soft to [].
    """
    from django.contrib.auth import get_user_model
    values = set()
    try:
        for user in get_user_model().objects.filter(is_active=True).select_related():
            profile = _get_profile(user)
            if not profile:
                continue
            for attr in PROFILE_ATTRS.get(concept, []):
                v = getattr(profile, attr, None)
                if v is None:
                    continue
                v = getattr(v, 'name', v)
                if str(v).strip():
                    values.add(str(v).strip())
                break
    except Exception:
        return []
    return sorted(values)
