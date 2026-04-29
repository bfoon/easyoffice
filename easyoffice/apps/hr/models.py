import uuid
from calendar import monthrange
from datetime import date, time, timedelta
from decimal import Decimal, ROUND_HALF_UP

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import models, transaction
from django.utils import timezone
from django.utils.text import slugify
from django.utils.translation import gettext_lazy as _
from apps.organization.models import Department, Unit, Position, OfficeLocation


def default_working_weekdays():
    # Python weekday numbers: Monday=0 ... Sunday=6
    return [0, 1, 2, 3, 4]


def default_allowances():
    return {}


def money(value):
    return Decimal(value or 0).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


def onboarding_upload_path(instance, filename):
    employee_code = instance.onboarding.employee_code or 'pending'
    return f'hr/onboarding/{employee_code}/{filename}'


class HRSetting(models.Model):
    """
    One-row HR policy table controlled by CEO/superuser.
    This removes hardcoded attendance/payroll rules from the views.
    """

    class PayCycle(models.TextChoices):
        MONTHLY = 'monthly', _('Monthly')
        BIWEEKLY = 'biweekly', _('Bi-weekly')
        WEEKLY = 'weekly', _('Weekly')

    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    normal_start_time = models.TimeField(default=time(8, 30))
    normal_end_time = models.TimeField(default=time(17, 0))
    work_hours_per_day = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('8.00'))
    working_weekdays = models.JSONField(default=default_working_weekdays)

    discipline_enabled = models.BooleanField(default=True)
    late_half_day_after_minutes = models.PositiveIntegerField(default=120)
    missing_attendance_is_absent = models.BooleanField(
        default=False,
        help_text='If enabled, a missing attendance record on a work day counts as unpaid/absent during payroll generation.'
    )

    pay_cycle = models.CharField(max_length=20, choices=PayCycle.choices, default=PayCycle.MONTHLY)
    payroll_day = models.PositiveSmallIntegerField(default=28)
    auto_generate_payroll = models.BooleanField(default=False)

    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='updated_hr_settings',
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'HR Setting'
        verbose_name_plural = 'HR Settings'

    def __str__(self):
        return 'HR Policy Settings'

    @classmethod
    def get_solo(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def is_working_day(self, target_date):
        return target_date.weekday() in set(self.working_weekdays or default_working_weekdays())

    def working_days_in_month(self, year, month):
        total_days = monthrange(year, month)[1]
        return [
            date(year, month, day)
            for day in range(1, total_days + 1)
            if self.is_working_day(date(year, month, day))
        ]


class HireCategory(models.Model):
    """
    CEO/superuser-controlled hire categories.
    Examples: Permanent Hire, Fixed Term Staff, Project Based Short Term, Day Hire.
    """

    class CategoryType(models.TextChoices):
        PERMANENT = 'permanent', _('Hire / Permanent')
        FIXED_TERM = 'fixed_term', _('Fixed Term Staff')
        PROJECT_BASED = 'project_based', _('Project Based Staff - Short Term')
        DAY_HIRE = 'day_hire', _('Day Hire')

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=120, unique=True)
    category_type = models.CharField(max_length=30, choices=CategoryType.choices)
    description = models.TextField(blank=True)
    requires_end_date = models.BooleanField(default=False)
    requires_number_of_days = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='created_hire_categories',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name


class EmployeeOnboarding(models.Model):
    """
    HR creates the employee profile first. IT/superuser updates the official email.
    HR then fills hire/pay details and submits the hire request to CEO for approval.
    """

    class Status(models.TextChoices):
        DRAFT = 'draft', _('Draft')
        IT_SETUP = 'it_setup', _('Waiting for IT Account Setup')
        HIRE_DRAFT = 'hire_draft', _('Hire Information Draft')
        CEO_REVIEW = 'ceo_review', _('Waiting for CEO Approval')
        REWORK = 'rework', _('Needs Rework')
        APPROVED = 'approved', _('Approved')
        DECLINED = 'declined', _('Declined')
        HIRED = 'hired', _('Hired / Staff Created')

    class Gender(models.TextChoices):
        MALE = 'male', _('Male')
        FEMALE = 'female', _('Female')
        OTHER = 'other', _('Other')

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    employee_code = models.CharField(max_length=40, unique=True, blank=True)

    first_name = models.CharField(max_length=80)
    middle_name = models.CharField(max_length=80, blank=True)
    last_name = models.CharField(max_length=80)
    gender = models.CharField(max_length=20, choices=Gender.choices, blank=True)
    date_of_birth = models.DateField(null=True, blank=True)
    nationality = models.CharField(max_length=80, blank=True)
    personal_email = models.EmailField(blank=True)
    phone = models.CharField(max_length=40, blank=True)
    alternative_phone = models.CharField(max_length=40, blank=True)

    proposed_username = models.CharField(max_length=150, blank=True)
    official_email = models.EmailField(blank=True)
    account_ready = models.BooleanField(default=False)
    account_updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='it_updated_onboarding_accounts',
    )
    account_updated_at = models.DateTimeField(null=True, blank=True)

    address_line = models.CharField(max_length=255, blank=True)
    city = models.CharField(max_length=100, blank=True)
    region = models.CharField(max_length=100, blank=True)
    country = models.CharField(max_length=100, default='The Gambia')

    hire_category = models.ForeignKey(HireCategory, on_delete=models.SET_NULL, null=True, blank=True)
    # Legacy text fields are kept so existing databases do not lose data.
    # New linked fields connect HR onboarding to the Organization and Staff apps.
    department = models.CharField(max_length=150, blank=True, help_text='Legacy/free-text department label')
    department_ref = models.ForeignKey(
        Department,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='hr_onboardings',
    )
    unit = models.ForeignKey(
        Unit,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='hr_onboardings',
    )
    position = models.ForeignKey(
        Position,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='hr_onboardings',
    )
    location = models.ForeignKey(
        OfficeLocation,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='hr_onboardings',
    )
    job_title = models.CharField(max_length=150, blank=True, help_text='Legacy/free-text job title label')
    supervisor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='pending_supervisees',
    )
    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)
    day_hire_days = models.PositiveIntegerField(default=0)
    salary = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))
    allowances = models.JSONField(default=default_allowances, blank=True)

    status = models.CharField(max_length=30, choices=Status.choices, default=Status.IT_SETUP)
    hr_notes = models.TextField(blank=True)
    ceo_comments = models.TextField(blank=True)
    decline_reason = models.TextField(blank=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name='created_employee_onboardings',
    )
    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='submitted_employee_onboardings',
    )
    submitted_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='approved_employee_onboardings',
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    staff_user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='source_onboarding',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['status']),
            models.Index(fields=['employee_code']),
            models.Index(fields=['official_email']),
        ]

    def __str__(self):
        return f'{self.full_name} ({self.employee_code or "Pending"})'

    @property
    def full_name(self):
        parts = [self.first_name, self.middle_name, self.last_name]
        return ' '.join([p for p in parts if p]).strip()

    @property
    def department_display(self):
        return self.department_ref.name if self.department_ref_id else self.department

    @property
    def job_title_display(self):
        return self.position.title if self.position_id else self.job_title

    def _next_employee_code(self):
        year = timezone.now().year
        prefix = f'EO-{year}-'
        last = EmployeeOnboarding.objects.filter(employee_code__startswith=prefix).order_by('-employee_code').first()
        if not last:
            return f'{prefix}0001'
        try:
            number = int(last.employee_code.split('-')[-1]) + 1
        except (ValueError, IndexError):
            number = EmployeeOnboarding.objects.filter(employee_code__startswith=prefix).count() + 1
        return f'{prefix}{number:04d}'

    def _build_username(self):
        UserModel = get_user_model()
        raw_base = f'{self.first_name}.{self.last_name}'.strip('.')
        base = slugify(raw_base).replace('-', '.') or f'staff.{uuid.uuid4().hex[:6]}'
        candidate = base
        counter = 1
        while (
            UserModel.objects.filter(username=candidate).exists()
            or EmployeeOnboarding.objects.filter(proposed_username=candidate).exclude(pk=self.pk).exists()
        ):
            counter += 1
            candidate = f'{base}{counter}'
        return candidate

    def save(self, *args, **kwargs):
        if not self.employee_code:
            self.employee_code = self._next_employee_code()
        if not self.proposed_username:
            self.proposed_username = self._build_username()
        super().save(*args, **kwargs)

    def validate_hire_ready(self):
        missing = []
        if not self.hire_category:
            missing.append('hire category')
        if not (self.department_ref_id or self.department):
            missing.append('department')
        if not (self.position_id or self.job_title):
            missing.append('position / job title')
        if not self.start_date:
            missing.append('start date')
        if self.salary is None:
            missing.append('salary')
        if self.hire_category and self.hire_category.requires_end_date and not self.end_date:
            missing.append('end date')
        if self.hire_category and self.hire_category.requires_number_of_days and not self.day_hire_days:
            missing.append('number of days')
        if missing:
            raise ValueError('Missing required hire information: ' + ', '.join(missing))

    def mark_account_ready(self, user, official_email, username=None):
        self.official_email = official_email
        if username:
            self.proposed_username = username
        self.account_ready = True
        self.account_updated_by = user
        self.account_updated_at = timezone.now()
        if self.status == self.Status.IT_SETUP:
            self.status = self.Status.HIRE_DRAFT
        self.save()

    def submit_for_ceo(self, user):
        self.validate_hire_ready()
        self.status = self.Status.CEO_REVIEW
        self.submitted_by = user
        self.submitted_at = timezone.now()
        self.save()

    @transaction.atomic
    def approve_and_create_staff(self, user):
        if not self.account_ready or not self.official_email:
            raise ValueError('IT must update the official email before CEO approval can create the staff account.')
        self.validate_hire_ready()

        UserModel = get_user_model()
        staff_user = self.staff_user
        if not staff_user:
            staff_user = UserModel.objects.filter(email__iexact=self.official_email).first()

        if not staff_user:
            staff_user = UserModel.objects.create_user(
                username=self.proposed_username,
                email=self.official_email,
                password=None,
            )

        staff_user.first_name = self.first_name
        staff_user.last_name = self.last_name
        staff_user.email = self.official_email
        staff_user.username = self.proposed_username
        staff_user.is_active = True
        if hasattr(staff_user, 'status'):
            staff_user.status = 'active'
        staff_user.save()

        self.staff_user = staff_user
        self.status = self.Status.HIRED
        self.approved_by = user
        self.approved_at = timezone.now()
        self.save()

        employment, _ = EmployeeEmployment.objects.update_or_create(
            user=staff_user,
            defaults={
                'onboarding': self,
                'employee_code': self.employee_code,
                'hire_category': self.hire_category,
                'department': self.department_display or '',
                'department_ref': self.department_ref,
                'unit': self.unit,
                'position': self.position,
                'location': self.location,
                'job_title': self.job_title_display or '',
                'supervisor': self.supervisor,
                'start_date': self.start_date,
                'end_date': self.end_date,
                'day_hire_days': self.day_hire_days,
                'monthly_salary': self.salary,
                'allowances': self.allowances or {},
                'status': EmployeeEmployment.Status.ACTIVE,
            }
        )
        employment.sync_staff_profile_from_employment()
        return staff_user


class EmployeeDocument(models.Model):
    class DocumentType(models.TextChoices):
        IDENTITY = 'identity', _('Identity Document')
        CV = 'cv', _('CV / Resume')
        CERTIFICATE = 'certificate', _('Certificate')
        CONTRACT = 'contract', _('Contract')
        OTHER = 'other', _('Other')

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    onboarding = models.ForeignKey(EmployeeOnboarding, on_delete=models.CASCADE, related_name='documents')
    document_type = models.CharField(max_length=30, choices=DocumentType.choices)
    title = models.CharField(max_length=150)
    file = models.FileField(upload_to=onboarding_upload_path)
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['document_type', 'title']

    def __str__(self):
        return f'{self.onboarding.full_name} - {self.title}'


class EmployeeWorkHistory(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    onboarding = models.ForeignKey(EmployeeOnboarding, on_delete=models.CASCADE, related_name='work_history')
    employer = models.CharField(max_length=180)
    job_title = models.CharField(max_length=150, blank=True)
    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)
    responsibilities = models.TextField(blank=True)

    class Meta:
        ordering = ['-start_date']

    def __str__(self):
        return f'{self.employer} - {self.job_title}'


class EmployeeReference(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    onboarding = models.ForeignKey(EmployeeOnboarding, on_delete=models.CASCADE, related_name='references')
    name = models.CharField(max_length=150)
    relationship = models.CharField(max_length=100, blank=True)
    organization = models.CharField(max_length=150, blank=True)
    phone = models.CharField(max_length=40, blank=True)
    email = models.EmailField(blank=True)
    notes = models.TextField(blank=True)

    def __str__(self):
        return f'{self.name} ({self.relationship})'


class EmployeeEmergencyContact(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    onboarding = models.ForeignKey(EmployeeOnboarding, on_delete=models.CASCADE, related_name='emergency_contacts')
    name = models.CharField(max_length=150)
    relationship = models.CharField(max_length=100, blank=True)
    phone = models.CharField(max_length=40)
    alternative_phone = models.CharField(max_length=40, blank=True)
    address = models.CharField(max_length=255, blank=True)

    def __str__(self):
        return f'{self.name} - {self.phone}'


class EmployeeEmployment(models.Model):
    class Status(models.TextChoices):
        ACTIVE = 'active', _('Active')
        ON_LEAVE = 'on_leave', _('On Leave')
        SUSPENDED = 'suspended', _('Suspended')
        RESIGNED = 'resigned', _('Resigned')
        TERMINATED = 'terminated', _('Terminated')
        ENDED = 'ended', _('Contract Ended')

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='employment')
    onboarding = models.OneToOneField(EmployeeOnboarding, on_delete=models.SET_NULL, null=True, blank=True, related_name='employment')
    employee_code = models.CharField(max_length=40, unique=True)
    hire_category = models.ForeignKey(HireCategory, on_delete=models.SET_NULL, null=True, blank=True)
    # Legacy labels plus linked organization fields.
    department = models.CharField(max_length=150, blank=True, help_text='Legacy/free-text department label')
    department_ref = models.ForeignKey(
        Department,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='hr_employments',
    )
    unit = models.ForeignKey(
        Unit,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='hr_employments',
    )
    position = models.ForeignKey(
        Position,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='hr_employments',
    )
    location = models.ForeignKey(
        OfficeLocation,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='hr_employments',
    )
    job_title = models.CharField(max_length=150, blank=True, help_text='Legacy/free-text job title label')
    supervisor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='direct_reports')

    start_date = models.DateField()
    end_date = models.DateField(null=True, blank=True)
    day_hire_days = models.PositiveIntegerField(default=0)
    monthly_salary = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))
    allowances = models.JSONField(default=default_allowances, blank=True)
    leave_balance_days = models.DecimalField(max_digits=6, decimal_places=2, default=Decimal('0.00'))

    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ACTIVE)
    resignation_date = models.DateField(null=True, blank=True)
    notice_given_date = models.DateField(null=True, blank=True)
    notice_end_date = models.DateField(null=True, blank=True)
    salary_paused = models.BooleanField(default=False)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['user__first_name', 'user__last_name']
        indexes = [models.Index(fields=['status']), models.Index(fields=['department'])]

    def __str__(self):
        return f'{self.user.get_full_name() or self.user.username} - {self.job_title_display}'

    @property
    def department_display(self):
        return self.department_ref.name if self.department_ref_id else self.department

    @property
    def job_title_display(self):
        return self.position.title if self.position_id else self.job_title

    def staff_contract_type(self):
        category_type = getattr(self.hire_category, 'category_type', '')
        mapping = {
            'permanent': 'permanent',
            'fixed_term': 'contract',
            'project_based': 'contract',
            'day_hire': 'part_time',
        }
        return mapping.get(category_type, 'contract')

    def sync_staff_profile_from_employment(self):
        """Keep apps.staff.StaffProfile aligned with HR approval and employment changes."""
        from apps.staff.models import StaffProfile

        profile, _ = StaffProfile.objects.get_or_create(user=self.user)
        profile.department = self.department_ref
        profile.unit = self.unit
        profile.position = self.position
        profile.location = self.location
        profile.supervisor = self.supervisor
        profile.contract_type = self.staff_contract_type()
        profile.contract_start = self.start_date
        profile.contract_end = self.end_date
        profile.office_email = self.user.email or profile.office_email
        profile.resignation_date = self.resignation_date
        if self.resignation_date and self.notes:
            profile.resignation_reason = self.notes
        profile.save()
        return profile

    def allowance_total(self):
        total = Decimal('0.00')
        for value in (self.allowances or {}).values():
            try:
                total += Decimal(str(value or 0))
            except Exception:
                continue
        return money(total)

    def resign(self, resignation_date=None, notice_given_date=None, notes=''):
        resignation_date = resignation_date or timezone.now().date()
        self.status = self.Status.RESIGNED
        self.resignation_date = resignation_date
        self.notice_given_date = notice_given_date or resignation_date
        self.notice_end_date = self.notice_given_date + timedelta(days=30)
        self.salary_paused = True
        if notes:
            self.notes = notes
        self.save()
        self.sync_staff_profile_from_employment()


class DisciplinaryAction(models.Model):
    class ActionType(models.TextChoices):
        WARNING = 'warning', _('Warning')
        QUERY = 'query', _('Query')
        UNPAID_LEAVE = 'unpaid_leave', _('Unpaid Leave')
        SUSPENSION = 'suspension', _('Suspension')
        TERMINATION_RECOMMENDATION = 'termination_recommendation', _('Termination Recommendation')

    class Status(models.TextChoices):
        DRAFT = 'draft', _('Draft')
        ACTIVE = 'active', _('Active')
        CLOSED = 'closed', _('Closed')
        CANCELLED = 'cancelled', _('Cancelled')

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    staff = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='disciplinary_actions')
    action_type = models.CharField(max_length=40, choices=ActionType.choices)
    reason = models.TextField()
    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)
    salary_paused = models.BooleanField(default=False)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ACTIVE)
    issued_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='issued_disciplinary_actions')
    created_at = models.DateTimeField(auto_now_add=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.staff} - {self.get_action_type_display()}'

    def affects_date(self, target_date):
        if self.status != self.Status.ACTIVE:
            return False
        if self.start_date and target_date < self.start_date:
            return False
        if self.end_date and target_date > self.end_date:
            return False
        return self.salary_paused or self.action_type in [self.ActionType.UNPAID_LEAVE, self.ActionType.SUSPENSION]


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
    staff = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='appraisals')
    supervisor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name='appraisals_given')
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
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
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
    staff = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='payroll_records')
    employment = models.ForeignKey(EmployeeEmployment, on_delete=models.SET_NULL, null=True, blank=True, related_name='payroll_records')
    period_month = models.PositiveSmallIntegerField()
    period_year = models.PositiveSmallIntegerField()
    basic_salary = models.DecimalField(max_digits=12, decimal_places=2)
    allowances = models.JSONField(default=dict)
    deductions = models.JSONField(default=dict)
    gross_salary = models.DecimalField(max_digits=12, decimal_places=2)
    net_salary = models.DecimalField(max_digits=12, decimal_places=2)
    tax_deducted = models.DecimalField(max_digits=10, decimal_places=2, default=0)

    total_work_days = models.DecimalField(max_digits=6, decimal_places=2, default=0)
    payable_days = models.DecimalField(max_digits=6, decimal_places=2, default=0)
    absent_days = models.DecimalField(max_digits=6, decimal_places=2, default=0)
    half_days = models.DecimalField(max_digits=6, decimal_places=2, default=0)
    unpaid_leave_days = models.DecimalField(max_digits=6, decimal_places=2, default=0)
    leave_payout = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    payment_date = models.DateField(null=True, blank=True)
    payment_reference = models.CharField(max_length=100, blank=True)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='approved_payrolls'
    )
    generated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='generated_payroll_records'
    )
    generated_at = models.DateTimeField(null=True, blank=True)
    payslip_sent_at = models.DateTimeField(null=True, blank=True)
    payslip_sent_to = models.EmailField(blank=True)
    payslip_last_error = models.TextField(blank=True)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ['staff', 'period_month', 'period_year']
        ordering = ['-period_year', '-period_month']

    def __str__(self):
        name = self.staff.get_full_name() or self.staff.username
        return f'{name} — {self.period_month}/{self.period_year}'

    def approve(self, user):
        self.status = self.Status.APPROVED
        self.approved_by = user
        self.save(update_fields=['status', 'approved_by'])

    def mark_paid(self, user, reference=''):
        if self.status == self.Status.DRAFT:
            self.approve(user)
        self.status = self.Status.PAID
        self.payment_date = timezone.now().date()
        if reference:
            self.payment_reference = reference
        self.save(update_fields=['status', 'payment_date', 'payment_reference', 'approved_by'])

    def mark_payslip_sent(self, email):
        self.payslip_sent_to = email or ''
        self.payslip_sent_at = timezone.now()
        self.payslip_last_error = ''
        self.save(update_fields=['payslip_sent_to', 'payslip_sent_at', 'payslip_last_error'])

    def mark_payslip_error(self, error):
        self.payslip_last_error = str(error)[:2000]
        self.save(update_fields=['payslip_last_error'])


class AttendanceRecord(models.Model):
    class Status(models.TextChoices):
        PRESENT = 'present', _('Present')
        LATE = 'late', _('Late')
        HALF_DAY = 'half_day', _('Half Day')
        ABSENT = 'absent', _('Absent')
        LEAVE = 'leave', _('On Leave')
        PAID_LEAVE = 'paid_leave', _('Paid Leave')
        UNPAID_LEAVE = 'unpaid_leave', _('Unpaid Leave')
        HOLIDAY = 'holiday', _('Holiday')
        REMOTE = 'remote', _('Remote')
        SUSPENDED = 'suspended', _('Suspended')

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    staff = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='attendance_records')
    date = models.DateField()
    check_in = models.TimeField(null=True, blank=True)
    check_out = models.TimeField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PRESENT)
    minutes_late = models.PositiveIntegerField(default=0)
    worked_hours = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    marked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
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
                self.worked_hours = money(Decimal(str(diff.total_seconds())) / Decimal('3600'))
            else:
                self.worked_hours = Decimal('0.00')
        else:
            self.worked_hours = Decimal('0.00')

    def apply_time_policy(self, setting=None):
        setting = setting or HRSetting.get_solo()
        self.calculate_worked_hours()

        if self.status not in [self.Status.PRESENT, self.Status.LATE, self.Status.REMOTE, self.Status.HALF_DAY]:
            self.minutes_late = 0
            return

        if not self.check_in:
            self.minutes_late = 0
            return

        official_minutes = setting.normal_start_time.hour * 60 + setting.normal_start_time.minute
        actual_minutes = self.check_in.hour * 60 + self.check_in.minute
        self.minutes_late = max(0, actual_minutes - official_minutes)

        if self.minutes_late > 0 and self.status == self.Status.PRESENT:
            self.status = self.Status.LATE

        if setting.discipline_enabled and self.minutes_late >= setting.late_half_day_after_minutes:
            self.status = self.Status.HALF_DAY
