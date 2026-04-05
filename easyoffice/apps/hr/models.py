import uuid
from datetime import timedelta
from django.db import models
from django.utils.translation import gettext_lazy as _
from apps.core.models import User


class PerformanceAppraisal(models.Model):
    class Status(models.TextChoices):
        DRAFT = 'draft', _('Draft')
        SELF_REVIEW = 'self_review', _('Self Review')
        SUPERVISOR_REVIEW = 'supervisor_review', _('Supervisor Review')
        HR_REVIEW = 'hr_review', _('HR Review')
        COMPLETED = 'completed', _('Completed')
        ACKNOWLEDGED = 'acknowledged', _('Acknowledged by Staff')

    class Rating(models.TextChoices):
        EXCEPTIONAL = '5', _('Exceptional')
        EXCEEDS = '4', _('Exceeds Expectations')
        MEETS = '3', _('Meets Expectations')
        BELOW = '2', _('Below Expectations')
        UNSATISFACTORY = '1', _('Unsatisfactory')

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    staff = models.ForeignKey(User, on_delete=models.CASCADE, related_name='appraisals')
    supervisor = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name='appraisals_given')
    period_start = models.DateField()
    period_end = models.DateField()
    year = models.PositiveSmallIntegerField()
    status = models.CharField(max_length=30, choices=Status.choices, default=Status.DRAFT)

    self_achievements = models.TextField(blank=True)
    self_challenges = models.TextField(blank=True)
    self_goals = models.TextField(blank=True)
    self_rating = models.CharField(max_length=2, choices=Rating.choices, blank=True)
    self_submitted_at = models.DateTimeField(null=True, blank=True)

    supervisor_comments = models.TextField(blank=True)
    supervisor_rating = models.CharField(max_length=2, choices=Rating.choices, blank=True)
    supervisor_submitted_at = models.DateTimeField(null=True, blank=True)
    areas_of_improvement = models.TextField(blank=True)
    training_recommended = models.TextField(blank=True)

    overall_rating = models.CharField(max_length=2, choices=Rating.choices, blank=True)
    hr_comments = models.TextField(blank=True)
    hr_reviewed_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='hr_appraisals'
    )
    hr_reviewed_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    acknowledged_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-year', 'staff__last_name']
        unique_together = ['staff', 'year']

    def __str__(self):
        return f'{self.staff.full_name} — {self.year} Appraisal'


class AppraisalKPI(models.Model):
    appraisal = models.ForeignKey(PerformanceAppraisal, on_delete=models.CASCADE, related_name='kpis')
    name = models.CharField(max_length=200)
    target = models.TextField()
    achievement = models.TextField(blank=True)
    weight = models.PositiveSmallIntegerField(default=10)
    self_score = models.PositiveSmallIntegerField(null=True, blank=True)
    supervisor_score = models.PositiveSmallIntegerField(null=True, blank=True)
    max_score = models.PositiveSmallIntegerField(default=100)

    def __str__(self):
        return f'{self.appraisal} — {self.name}'


class PayrollRecord(models.Model):
    class Status(models.TextChoices):
        DRAFT = 'draft', _('Draft')
        APPROVED = 'approved', _('Approved')
        PAID = 'paid', _('Paid')
        CANCELLED = 'cancelled', _('Cancelled')

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    staff = models.ForeignKey(User, on_delete=models.CASCADE, related_name='payroll_records')
    period_month = models.PositiveSmallIntegerField()
    period_year = models.PositiveSmallIntegerField()
    basic_salary = models.DecimalField(max_digits=12, decimal_places=2)
    allowances = models.JSONField(default=dict)
    deductions = models.JSONField(default=dict)
    gross_salary = models.DecimalField(max_digits=12, decimal_places=2)
    net_salary = models.DecimalField(max_digits=12, decimal_places=2)
    tax_deducted = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    payment_date = models.DateField(null=True, blank=True)
    payment_reference = models.CharField(max_length=100, blank=True)
    approved_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='approved_payrolls'
    )
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ['staff', 'period_month', 'period_year']
        ordering = ['-period_year', '-period_month']

    def __str__(self):
        return f'{self.staff.full_name} — {self.period_month}/{self.period_year}'


class AttendanceRecord(models.Model):
    class Status(models.TextChoices):
        PRESENT = 'present', _('Present')
        LATE = 'late', _('Late')
        ABSENT = 'absent', _('Absent')
        LEAVE = 'leave', _('On Leave')
        HOLIDAY = 'holiday', _('Holiday')
        REMOTE = 'remote', _('Remote')

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    staff = models.ForeignKey(User, on_delete=models.CASCADE, related_name='attendance_records')
    date = models.DateField()
    check_in = models.TimeField(null=True, blank=True)
    check_out = models.TimeField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PRESENT)
    minutes_late = models.PositiveIntegerField(default=0)
    worked_hours = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    marked_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='marked_attendance_records'
    )
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ['staff', 'date']
        ordering = ['-date', 'staff__last_name', 'staff__first_name']

    def __str__(self):
        return f'{self.staff.full_name} - {self.date}'

    def calculate_worked_hours(self):
        if self.check_in and self.check_out:
            start = timedelta(hours=self.check_in.hour, minutes=self.check_in.minute)
            end = timedelta(hours=self.check_out.hour, minutes=self.check_out.minute)
            diff = end - start
            if diff.total_seconds() > 0:
                self.worked_hours = round(diff.total_seconds() / 3600, 2)
            else:
                self.worked_hours = 0
        else:
            self.worked_hours = 0