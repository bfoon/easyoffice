import uuid
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
    completed_at = models.DateTimeField(null=True, blank=True)

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
