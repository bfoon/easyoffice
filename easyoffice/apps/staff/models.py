import uuid
from django.db import models
from django.utils.translation import gettext_lazy as _
from apps.core.models import User
from apps.organization.models import Unit, Position, Department, OfficeLocation



class StaffProfile(models.Model):
    """Extended staff information linked to User."""
    class ContractType(models.TextChoices):
        PERMANENT = 'permanent', _('Permanent')
        CONTRACT = 'contract', _('Contract')
        PART_TIME = 'part_time', _('Part Time')
        CONSULTANT = 'consultant', _('Consultant')
        INTERN = 'intern', _('Intern')
        VOLUNTEER = 'volunteer', _('Volunteer')

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='staffprofile')
    unit = models.ForeignKey(Unit, on_delete=models.SET_NULL, null=True, blank=True)
    department = models.ForeignKey(Department, on_delete=models.SET_NULL, null=True, blank=True)
    position = models.ForeignKey(Position, on_delete=models.SET_NULL, null=True, blank=True)
    supervisor = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='supervisees'
    )
    location = models.ForeignKey(OfficeLocation, on_delete=models.SET_NULL, null=True, blank=True)
    contract_type = models.CharField(max_length=20, choices=ContractType.choices, default=ContractType.PERMANENT)
    contract_start = models.DateField(null=True, blank=True)
    contract_end = models.DateField(null=True, blank=True)
    national_id = models.CharField(max_length=50, blank=True)
    tax_id = models.CharField(max_length=50, blank=True)
    bank_account = models.CharField(max_length=50, blank=True)
    bank_name = models.CharField(max_length=100, blank=True)
    emergency_contact_name = models.CharField(max_length=200, blank=True)
    emergency_contact_phone = models.CharField(max_length=30, blank=True)
    emergency_contact_relation = models.CharField(max_length=50, blank=True)
    date_of_birth = models.DateField(null=True, blank=True)
    gender = models.CharField(max_length=20, blank=True)
    nationality = models.CharField(max_length=100, blank=True)
    office_email = models.EmailField(blank=True)
    office_extension = models.CharField(max_length=20, blank=True)
    work_schedule = models.JSONField(default=dict, blank=True)
    is_supervisor_role = models.BooleanField(default=False)
    probation_end = models.DateField(null=True, blank=True)
    resignation_date = models.DateField(null=True, blank=True)
    resignation_reason = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['user__last_name']

    def __str__(self):
        return f'{self.user.full_name} — {self.position or "No Position"}'

    @property
    def supervisees_list(self):
        return User.objects.filter(staffprofile__supervisor=self.user, status='active')

    @property
    def years_of_service(self):
        if self.contract_start:
            from django.utils import timezone
            delta = timezone.now().date() - self.contract_start
            return round(delta.days / 365.25, 1)
        return 0

    def can_approve_leave_for(self, staff_user):
        """
        Approval rule:
        - superuser: can approve all
        - direct supervisor: can approve own direct supervisees
        - HR: if also the recorded supervisor and same unit, can approve
        """
        profile_owner = self.user

        if profile_owner.is_superuser:
            return True

        try:
            target_profile = staff_user.staffprofile
        except StaffProfile.DoesNotExist:
            return False

        if target_profile.supervisor_id != profile_owner.id:
            return False

        if self.unit_id and target_profile.unit_id and self.unit_id != target_profile.unit_id:
            return False

        return True


class LeaveType(models.Model):
    name = models.CharField(max_length=100)
    code = models.CharField(max_length=20, unique=True)
    days_per_year = models.PositiveIntegerField(default=0)
    is_paid = models.BooleanField(default=True)
    requires_approval = models.BooleanField(default=True)
    carry_forward = models.BooleanField(default=False)
    max_carry_forward_days = models.PositiveIntegerField(default=0)
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return self.name


class LeaveRequest(models.Model):
    class Status(models.TextChoices):
        PENDING = 'pending', _('Pending')
        APPROVED = 'approved', _('Approved')
        REJECTED = 'rejected', _('Rejected')
        CANCELLED = 'cancelled', _('Cancelled')

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    staff = models.ForeignKey(User, on_delete=models.CASCADE, related_name='leave_requests')
    leave_type = models.ForeignKey(LeaveType, on_delete=models.CASCADE)
    start_date = models.DateField()
    end_date = models.DateField()
    days_requested = models.PositiveIntegerField()
    reason = models.TextField()
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    approved_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True,
                                    related_name='approved_leaves')
    approval_date = models.DateTimeField(null=True, blank=True)
    approval_notes = models.TextField(blank=True)
    supporting_document = models.FileField(upload_to='leave_docs/', null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.staff.full_name} — {self.leave_type} ({self.start_date} to {self.end_date})'


class LeaveBalance(models.Model):
    """
    Tracks each staff member's leave entitlement and usage per leave type per year.
    Created/updated automatically when leaves are approved or on yearly rollover.
    """
    staff = models.ForeignKey(User, on_delete=models.CASCADE, related_name='leave_balances')
    leave_type = models.ForeignKey(LeaveType, on_delete=models.CASCADE, related_name='balances')
    year = models.PositiveIntegerField()
    days_entitled = models.PositiveIntegerField(default=0)
    days_used = models.PositiveIntegerField(default=0)
    days_carried_forward = models.PositiveIntegerField(default=0)

    class Meta:
        unique_together = ['staff', 'leave_type', 'year']
        ordering = ['-year', 'leave_type__name']

    def __str__(self):
        return f'{self.staff.full_name} / {self.leave_type.name} / {self.year}'

    @property
    def days_remaining(self):
        return max(0, self.days_entitled + self.days_carried_forward - self.days_used)

    @property
    def days_pending(self):
        """Days currently under a pending leave request for this type/year."""
        return self.staff.leave_requests.filter(
            leave_type=self.leave_type,
            status='pending',
            start_date__year=self.year,
        ).aggregate(
            total=models.Sum('days_requested')
        )['total'] or 0

    @classmethod
    def get_or_create_for(cls, staff_user, leave_type, year=None):
        from django.utils import timezone
        if year is None:
            year = timezone.now().year
        balance, created = cls.objects.get_or_create(
            staff=staff_user,
            leave_type=leave_type,
            year=year,
            defaults={'days_entitled': leave_type.days_per_year},
        )
        return balance

    def recalculate_used(self):
        """Recompute days_used from approved leave requests for this year."""
        from django.db.models import Sum
        used = self.staff.leave_requests.filter(
            leave_type=self.leave_type,
            status='approved',
            start_date__year=self.year,
        ).aggregate(total=Sum('days_requested'))['total'] or 0
        self.days_used = used
        self.save(update_fields=['days_used'])