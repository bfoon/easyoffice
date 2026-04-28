import uuid
import json
from django.db import models
from django.utils.translation import gettext_lazy as _
from apps.core.models import User


class Project(models.Model):
    class Status(models.TextChoices):
        PLANNING = 'planning', _('Planning')
        ACTIVE = 'active', _('Active')
        ON_HOLD = 'on_hold', _('On Hold')
        COMPLETED = 'completed', _('Completed')
        CANCELLED = 'cancelled', _('Cancelled')

    class Priority(models.TextChoices):
        LOW = 'low', _('Low')
        MEDIUM = 'medium', _('Medium')
        HIGH = 'high', _('High')
        CRITICAL = 'critical', _('Critical')

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=300)
    code = models.CharField(max_length=20, unique=True)
    description = models.TextField(blank=True)
    department = models.ForeignKey('organization.Department', on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name='projects')
    units = models.ManyToManyField('organization.Unit', related_name='projects', blank=True)
    project_manager = models.ForeignKey(User, on_delete=models.SET_NULL, null=True,
                                        related_name='managed_projects')
    owner = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='owned_projects',
        help_text='The project owner — the only role allowed to edit core project fields. '
                  'Defaults to the creator. May be the same as the project manager.',
    )
    team_members = models.ManyToManyField(User, related_name='projects', blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PLANNING)
    priority = models.CharField(max_length=20, choices=Priority.choices, default=Priority.MEDIUM)
    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)
    budget = models.DecimalField(max_digits=15, decimal_places=2, null=True, blank=True)
    budget_spent = models.DecimalField(max_digits=15, decimal_places=2, default=0)
    progress_pct = models.PositiveSmallIntegerField(default=0)
    is_public = models.BooleanField(default=True,
                                    help_text='Visible to all units when True')
    tags = models.CharField(max_length=500, blank=True)
    cover_color = models.CharField(max_length=7, default='#1e3a5f')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'[{self.code}] {self.name}'


class Milestone(models.Model):
    class Status(models.TextChoices):
        PENDING = 'pending', _('Pending')
        IN_PROGRESS = 'in_progress', _('In Progress')
        COMPLETED = 'completed', _('Completed')
        MISSED = 'missed', _('Missed')

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='milestones')
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    due_date = models.DateField()
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    order = models.PositiveSmallIntegerField(default=0)

    weight_pct = models.PositiveSmallIntegerField(
        null=True, blank=True,
        help_text=(
            'How much this milestone is worth toward overall project progress, 0-100. '
            'Leave blank to share the remainder equally with other unweighted milestones.'
        ),
    )

    assigned_to = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='assigned_project_milestones'
    )
    task = models.OneToOneField(
        'tasks.Task',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='project_milestone'
    )
    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='created_project_milestones'
    )

    completed_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['order', 'due_date']

    def __str__(self):
        return f'{self.project.code} — {self.name}'


class ProjectUpdate(models.Model):
    """Notes/updates posted to a project by any team member."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='updates')
    author = models.ForeignKey(User, on_delete=models.CASCADE)
    title = models.CharField(max_length=200, blank=True)
    content = models.TextField()
    progress_at_time = models.PositiveSmallIntegerField(null=True, blank=True)
    status_at_time = models.CharField(max_length=20, blank=True)
    is_milestone_update = models.BooleanField(default=False)
    milestone = models.ForeignKey(Milestone, on_delete=models.SET_NULL, null=True, blank=True)
    attachments_count = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.project.code} update by {self.author}'


class Risk(models.Model):
    class Level(models.TextChoices):
        LOW = 'low', _('Low')
        MEDIUM = 'medium', _('Medium')
        HIGH = 'high', _('High')
        CRITICAL = 'critical', _('Critical')

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='risks')
    title = models.CharField(max_length=200)
    description = models.TextField()
    level = models.CharField(max_length=20, choices=Level.choices)
    mitigation = models.TextField(blank=True)
    owner = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    is_resolved = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f'{self.project.code} — {self.title}'


# =============================================================================
# PROJECT TOOLS
# =============================================================================

# ─── Quick Survey ─────────────────────────────────────────────────────────────

class Survey(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='surveys')
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    is_anonymous = models.BooleanField(
        default=False,
        help_text='When enabled, respondents are not recorded'
    )

    # ── Public access (no login required) ────────────────────────────────────
    is_public = models.BooleanField(
        default=False,
        help_text='Allow anyone with the link to respond without logging in'
    )
    public_token = models.UUIDField(
        null=True,
        blank=True,
        editable=False,
        help_text='Unique token used in the public share link'
    )
    allow_multiple_responses = models.BooleanField(
        default=False,
        help_text='Allow the same device/IP to submit more than once (public surveys only)'
    )

    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name='created_surveys')
    created_at = models.DateTimeField(auto_now_add=True)
    closes_at = models.DateField(null=True, blank=True, help_text='Leave blank to keep open indefinitely')

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.project.code} — {self.title}'

    @property
    def response_count(self):
        return self.responses.count()

    def get_public_url(self):
        from django.urls import reverse
        return reverse('survey_public', kwargs={'token': self.public_token})


class SurveyQuestion(models.Model):
    class Type(models.TextChoices):
        TEXT = 'text', _('Short Text')
        PARAGRAPH = 'paragraph', _('Paragraph')
        SINGLE_CHOICE = 'single_choice', _('Single Choice')
        MULTI_CHOICE = 'multi_choice', _('Multiple Choice')
        RATING = 'rating', _('Rating 1–5')
        YES_NO = 'yes_no', _('Yes / No')
        NUMBER = 'number', _('Number')
        GEOLOCATION = 'geolocation', _('Geolocation (GPS)')

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    survey = models.ForeignKey(Survey, on_delete=models.CASCADE, related_name='questions')
    text = models.CharField(max_length=400)
    q_type = models.CharField(max_length=20, choices=Type.choices, default=Type.TEXT)
    options = models.TextField(
        blank=True,
        help_text='For choice questions: enter one option per line'
    )
    is_required = models.BooleanField(default=True)
    order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ['order']

    def __str__(self):
        return f'[{self.survey.title}] Q{self.order}: {self.text[:60]}'

    def get_options_list(self):
        return [o.strip() for o in self.options.splitlines() if o.strip()]


class SurveyResponse(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    survey = models.ForeignKey(Survey, on_delete=models.CASCADE, related_name='responses')
    respondent = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        help_text='Null when anonymous or public'
    )
    respondent_name = models.CharField(max_length=120, blank=True)

    # ── Capture fields for public responses ──────────────────────────────────
    ip_address = models.GenericIPAddressField(
        null=True, blank=True,
        help_text='Captured from the request for public submissions'
    )
    device_id = models.CharField(
        max_length=128, blank=True,
        help_text='Browser fingerprint sent by the client (FingerprintJS)'
    )
    user_agent = models.TextField(blank=True)
    is_public_response = models.BooleanField(default=False)

    submitted_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-submitted_at']

    def __str__(self):
        return f'Response #{self.pk} to "{self.survey.title}"'


class SurveyAnswer(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    response = models.ForeignKey(SurveyResponse, on_delete=models.CASCADE, related_name='answers')
    question = models.ForeignKey(SurveyQuestion, on_delete=models.CASCADE, related_name='answers')
    value = models.TextField(blank=True)
    import_batch = models.ForeignKey(
            'LocationImport',
            on_delete=models.SET_NULL, null=True, blank=True,
            related_name='imported_answers',
            help_text='Set when this answer came from a bulk geo-import.'
        )

    def __str__(self):
        return f'Answer to Q:{self.question_id}'


# ─── Tracking Sheet ───────────────────────────────────────────────────────────

class TrackingSheet(models.Model):
    """One customisable spreadsheet per project."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.OneToOneField(Project, on_delete=models.CASCADE, related_name='tracking_sheet')
    title = models.CharField(max_length=200, default='Tracking Sheet')
    # JSON list of {key, label, type} dicts — type: text | number | date | status | checkbox
    columns_json = models.TextField(default='[]')
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name='created_sheets')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f'{self.project.code} — {self.title}'

    def get_columns(self):
        try:
            return json.loads(self.columns_json)
        except Exception:
            return []

    def set_columns(self, cols):
        self.columns_json = json.dumps(cols)


class TrackingSheetAccess(models.Model):
    class Permission(models.TextChoices):
        VIEW = 'view', 'View'
        INPUT = 'input', 'Input'
        EDIT = 'edit', 'Edit'
        FULL = 'full', 'Full'

    sheet = models.ForeignKey(TrackingSheet, on_delete=models.CASCADE, related_name='access_rows')
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='tracking_sheet_access')
    permission = models.CharField(max_length=10, choices=Permission.choices, default=Permission.INPUT)
    granted_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='granted_tracking_access')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [('sheet', 'user')]
        ordering = ['user__first_name', 'user__last_name']

    def __str__(self):
        return f'{self.sheet} -> {self.user} ({self.permission})'

    def get_data(self):
        try:
            return json.loads(self.data_json)
        except Exception:
            return {}

    def set_data(self, d):
        self.data_json = json.dumps(d)


class TrackingRow(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    sheet = models.ForeignKey(
        TrackingSheet,
        on_delete=models.CASCADE,
        related_name='rows'
    )
    order = models.PositiveIntegerField(default=0)
    data_json = models.JSONField(default=dict, blank=True)
    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True
    )
    created_at = models.DateTimeField(auto_now_add=True)

    def get_data(self):
        return self.data_json or {}

    def __str__(self):
        return f"{self.sheet.title} - Row {self.order}"

# ─── Location Map ─────────────────────────────────────────────────────────────

class ProjectZone(models.Model):
    """
    A named zone / sector that groups ProjectLocations together.
    When zone view is enabled on the map, every zone's locations are
    enclosed in a convex-hull polygon with the zone's colour.
    """
    project = models.ForeignKey('Project', on_delete=models.CASCADE, related_name='zones')
    name = models.CharField(max_length=100)
    color = models.CharField(max_length=7, default='#6366f1',
                             help_text='Hex colour for the polygon fill/border.')
    description = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return f'{self.project.name} – {self.name}'

class ProjectLocation(models.Model):
    class Status(models.TextChoices):
        PENDING = 'pending', _('Pending')
        IN_PROGRESS = 'in_progress', _('In Progress')
        COMPLETED = 'completed', _('Completed')
        ON_HOLD = 'on_hold', _('On Hold')

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='locations')
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    address = models.CharField(max_length=400, blank=True)
    latitude = models.DecimalField(max_digits=10, decimal_places=6)
    longitude = models.DecimalField(max_digits=10, decimal_places=6)
    category = models.CharField(
        max_length=100, blank=True,
        help_text='e.g. Installation, Office, Field Site, Warehouse'
    )
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    assigned_to = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='assigned_locations'
    )
    zone = models.ForeignKey(
              'ProjectZone',
              on_delete=models.SET_NULL,
              null=True, blank=True,
              related_name='locations',
          )
    notes = models.TextField(blank=True)
    import_batch = models.ForeignKey(
                'LocationImport',
                on_delete=models.SET_NULL, null=True, blank=True,
                related_name='imported_locations',
                help_text='Set when this row was created via bulk import.'
            )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return f'{self.project.code} — {self.name}'


class LocationImport(models.Model):
    """
    One row per import operation. When a user imports a CSV/XLSX into the
    location map (or into a survey's geolocation question), we create one
    of these so the rows can later be bulk-deleted or traced back to source.
    """

    class Target(models.TextChoices):
        PROJECT_LOCATIONS = 'locations', _('Project Locations')
        SURVEY_ANSWERS = 'survey', _('Survey Geo Answers')

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(
        Project, on_delete=models.CASCADE, related_name='location_imports'
    )
    target = models.CharField(
        max_length=20, choices=Target.choices, default=Target.PROJECT_LOCATIONS
    )

    # When target=SURVEY_ANSWERS, these identify which survey and which geo question
    survey = models.ForeignKey(
        Survey, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='geo_imports'
    )
    survey_question = models.ForeignKey(
        SurveyQuestion, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='geo_imports'
    )

    # File linkage — SET_NULL so the row survives if the file is later deleted
    source_file = models.ForeignKey(
        'files.SharedFile',
        on_delete=models.SET_NULL, null=True, blank=True,
        related_name='location_imports',
        help_text='Link to the source SharedFile. Cleared when user unlinks.'
    )
    # Preserved even after unlink — so history stays readable
    source_filename = models.CharField(max_length=255, blank=True)
    source_filesize = models.PositiveIntegerField(default=0)

    # The column mapping used — also cached per-file for "remember mappings"
    # Shape: {"name": "Site Name", "latitude": "Lat", "longitude": "Lng", ...}
    column_mapping = models.JSONField(default=dict, blank=True)

    # How many rows were successfully created
    row_count = models.PositiveIntegerField(default=0)
    # How many rows were skipped (bad coords, duplicate, etc.)
    skipped_count = models.PositiveIntegerField(default=0)

    # Free-form notes about the import (errors, warnings)
    notes = models.TextField(blank=True)

    imported_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='location_imports'
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        label = self.source_filename or '(unknown file)'
        return f'{self.project.code} import · {label} · {self.row_count} rows'

    @property
    def file_still_linked(self):
        return self.source_file_id is not None

    def get_mapping_display(self):
        """Human-readable summary of what columns map to what."""
        if not self.column_mapping:
            return '—'
        bits = [f'{k} ← "{v}"' for k, v in self.column_mapping.items() if v]
        return ', '.join(bits[:4]) + ('…' if len(bits) > 4 else '')