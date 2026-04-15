from django.core.management.base import BaseCommand
from django.contrib.auth import get_user_model
from apps.organization.models import Department, Unit, Position, OfficeLocation
from apps.staff.models import LeaveType
from apps.core.models import OfficeSettings

User = get_user_model()

class Command(BaseCommand):
    help = 'Create default data for EasyOffice'

    def handle(self, *args, **kwargs):
        self.stdout.write('Creating default data...')
        self._create_office_settings()
        self._create_location()
        self._create_departments()
        self._create_leave_types()
        self._create_superuser()
        self.stdout.write(self.style.SUCCESS('Default data created successfully!'))

    def _create_office_settings(self):
        defaults = [
            ('office_name', 'EasyOffice', 'string', 'Office Name', 'branding'),
            ('office_tagline', 'Your Virtual Office', 'string', 'Office Tagline', 'branding'),
            ('primary_color', '#1e3a5f', 'color', 'Primary Color', 'branding'),
            ('accent_color', '#2196f3', 'color', 'Accent Color', 'branding'),
            ('currency', 'GMD', 'string', 'Currency Code', 'finance'),
            ('currency_symbol', 'D', 'string', 'Currency Symbol', 'finance'),
            ('fiscal_year_start', '1', 'integer', 'Fiscal Year Start Month', 'finance'),
            ('appraisal_cycle', '12', 'integer', 'Appraisal Cycle (months)', 'hr'),
            ('working_hours_start', '08:00', 'string', 'Working Hours Start', 'general'),
            ('working_hours_end', '17:00', 'string', 'Working Hours End', 'general'),
            ('max_file_upload_mb', '50', 'integer', 'Max File Upload Size (MB)', 'files'),
            ('enable_chat', 'true', 'boolean', 'Enable Chat', 'features'),
            ('enable_payroll', 'true', 'boolean', 'Enable Payroll', 'features'),
            ('enable_appraisals', 'true', 'boolean', 'Enable Appraisals', 'features'),
        ]
        for key, value, vtype, label, group in defaults:
            OfficeSettings.objects.get_or_create(key=key, defaults={
                'value': value, 'value_type': vtype, 'label': label, 'group': group
            })
        self.stdout.write('  ✓ Office settings')

    def _create_location(self):
        OfficeLocation.objects.get_or_create(
            name='Head Office',
            defaults={'city': 'Banjul', 'country': 'The Gambia',
                      'is_headquarters': True, 'timezone': 'Africa/Banjul'}
        )
        self.stdout.write('  ✓ Office location')

    def _create_departments(self):
        depts = [
            ('Executive Office', 'EXEC', '#1e3a5f', 'bi-star'),
            ('Human Resources', 'HR', '#7c3aed', 'bi-people'),
            ('Finance', 'FIN', '#059669', 'bi-currency-dollar'),
            ('Information Technology', 'IT', '#0284c7', 'bi-laptop'),
            ('Sales & Marketing', 'SALES', '#dc2626', 'bi-graph-up'),
            ('Operations', 'OPS', '#d97706', 'bi-gear'),
            ('Technical', 'TECH', '#0891b2', 'bi-wrench'),
        ]
        for name, code, color, icon in depts:
            dept, created = Department.objects.get_or_create(code=code, defaults={
                'name': name, 'color': color, 'icon': icon
            })
            if created:
                Unit.objects.create(
                    name=f'{name} Unit',
                    code=f'{code}-UNIT',
                    department=dept,
                    color=color,
                )
        self.stdout.write('  ✓ Departments & units')

    def _create_leave_types(self):
        leaves = [
            ('Annual Leave', 'AL', 21, True, True, 5),
            ('Sick Leave', 'SL', 14, True, False, 0),
            ('Maternity Leave', 'ML', 84, True, False, 0),
            ('Paternity Leave', 'PL', 5, True, False, 0),
            ('Compassionate Leave', 'CL', 5, True, False, 0),
            ('Study Leave', 'STL', 10, True, False, 0),
            ('Unpaid Leave', 'UL', 0, False, False, 0),
        ]
        for name, code, days, paid, carry, max_carry in leaves:
            LeaveType.objects.get_or_create(code=code, defaults={
                'name': name, 'days_per_year': days, 'is_paid': paid,
                'carry_forward': carry, 'max_carry_forward_days': max_carry
            })
        self.stdout.write('  ✓ Leave types')

    # def _create_superuser(self):
    #     if not User.objects.filter(email='office.administrator@easysolutions.gm').exists():
    #         admin = User.objects.create_superuser(
    #             username='officeadmin',
    #             email='office.administrator@easysolutions.gm',
    #             password='Admin@123!',
    #             first_name='Office',
    #             last_name='Administrator',
    #         )
    #         self.stdout.write(f'  ✓ Admin user created: admin@easyoffice.com / Admin@123!')
    #     else:
    #         self.stdout.write('  ✓ Admin user already exists')
