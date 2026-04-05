import uuid
from django.db import models
from django.utils.translation import gettext_lazy as _
from apps.core.models import User


class Department(models.Model):
    class ManagementLevel(models.IntegerChoices):
        LEVEL_1 = 1, _('Level 1')
        LEVEL_2 = 2, _('Level 2')

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=200)
    code = models.CharField(max_length=20, unique=True)
    description = models.TextField(blank=True)
    head = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='headed_departments'
    )
    parent = models.ForeignKey(
        'self',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='sub_departments'
    )
    management_level = models.PositiveSmallIntegerField(
        choices=ManagementLevel.choices,
        default=ManagementLevel.LEVEL_2
    )
    row = models.PositiveIntegerField(default=1, db_index=True)
    col = models.PositiveIntegerField(default=1, db_index=True)
    sort_order = models.PositiveIntegerField(default=0, db_index=True)
    color = models.CharField(max_length=7, default='#2196f3')
    icon = models.CharField(max_length=50, default='bi-building')
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['management_level', 'row', 'col', 'sort_order', 'name']

    def __str__(self):
        return self.name

    def get_all_units(self):
        return self.units.filter(is_active=True).order_by('row', 'col', 'sort_order', 'name')


class Unit(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=200)
    code = models.CharField(max_length=20, unique=True)
    department = models.ForeignKey(
        Department,
        on_delete=models.CASCADE,
        related_name='units'
    )
    head_of_unit = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='led_units'
    )
    description = models.TextField(blank=True)
    row = models.PositiveIntegerField(default=1, db_index=True)
    col = models.PositiveIntegerField(default=1, db_index=True)
    sort_order = models.PositiveIntegerField(default=0, db_index=True)
    color = models.CharField(max_length=7, default='#4caf50')
    icon = models.CharField(max_length=50, default='bi-people')
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    objectives = models.TextField(blank=True)
    budget_code = models.CharField(max_length=50, blank=True)

    class Meta:
        ordering = ['department__management_level', 'department__row', 'department__col', 'row', 'col', 'sort_order', 'name']

    def __str__(self):
        return f'{self.department.code} — {self.name}'

    @property
    def member_count(self):
        return self.staffprofile_set.filter(user__status='active').count()


class Position(models.Model):
    class Grade(models.TextChoices):
        ENTRY = 'entry', _('Entry Level')
        JUNIOR = 'junior', _('Junior')
        MID = 'mid', _('Mid Level')
        SENIOR = 'senior', _('Senior')
        LEAD = 'lead', _('Lead')
        MANAGER = 'manager', _('Manager')
        DIRECTOR = 'director', _('Director')
        EXECUTIVE = 'executive', _('Executive')

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    title = models.CharField(max_length=200)
    code = models.CharField(max_length=20, unique=True)
    department = models.ForeignKey(Department, on_delete=models.SET_NULL, null=True, blank=True)
    grade = models.CharField(max_length=20, choices=Grade.choices, default=Grade.MID)
    description = models.TextField(blank=True)
    responsibilities = models.TextField(blank=True)
    is_supervisor = models.BooleanField(default=False)
    salary_min = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    salary_max = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ['title']

    def __str__(self):
        return f'{self.title} ({self.get_grade_display()})'


class OfficeLocation(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=200)
    address = models.TextField(blank=True)
    city = models.CharField(max_length=100, blank=True)
    country = models.CharField(max_length=100, blank=True)
    timezone = models.CharField(max_length=50, default='UTC')
    is_headquarters = models.BooleanField(default=False)
    is_virtual = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return self.name