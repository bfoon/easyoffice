"""Seed the lookup tables that signal handlers depend on.

Idempotent — safe to run repeatedly. Run once after migrating:

    manage.py seed_finance
"""
from django.core.management.base import BaseCommand

from apps.finance.models import Department, FinanceCategory


REVENUE_CATEGORIES = [
    ('SALES',   'Sales'),                # invoice handler depends on this
    ('PAYMENT', 'Payments Received'),    # payment-in handler depends on this
    ('OTHER',   'Other Income'),
]

EXPENSE_CATEGORIES = [
    # COGS
    ('COGS_GOODS',  'Cost of Goods Sold', True),
    ('COGS_LABOUR', 'Direct Labour',      True),
    # Operating
    ('PAYMENT',  'Payments Made', False),  # payment-out handler depends on this
    ('SALARIES', 'Salaries',      False),
    ('RENT',     'Rent',          False),
    ('UTILITIES','Utilities',     False),
    ('MARKETING','Marketing',     False),
    ('TRAVEL',   'Travel',        False),
    ('SOFTWARE', 'Software & Subscriptions', False),
    ('FEES',     'Bank & Professional Fees', False),
    ('OTHER',    'Other Expenses', False),
]

DEPARTMENTS = [
    ('OPS',       'Operations'),
    ('SALES',     'Sales'),
    ('FINANCE',   'Finance'),
    ('TECH',      'Technology'),
    ('HR',        'Human Resources'),
]


class Command(BaseCommand):
    help = 'Seed default categories and departments.'

    def handle(self, *args, **options):
        for code, name in REVENUE_CATEGORIES:
            FinanceCategory.objects.get_or_create(
                kind=FinanceCategory.Kind.REVENUE, code=code,
                defaults={'name': name, 'is_active': True},
            )
        for code, name, is_cogs in EXPENSE_CATEGORIES:
            FinanceCategory.objects.get_or_create(
                kind=FinanceCategory.Kind.EXPENSE, code=code,
                defaults={'name': name, 'is_cogs': is_cogs, 'is_active': True},
            )
        for code, name in DEPARTMENTS:
            Department.objects.get_or_create(
                code=code, defaults={'name': name, 'is_active': True},
            )

        self.stdout.write(self.style.SUCCESS(
            f'Seeded {FinanceCategory.objects.count()} categories, '
            f'{Department.objects.count()} departments.'
        ))
