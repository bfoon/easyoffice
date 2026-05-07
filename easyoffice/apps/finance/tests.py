"""
Sanity tests for the finance app.

These cover the highest-risk paths: signal handlers, audit diff capture,
anomaly detection, and KPI math. Run with:

    manage.py test apps.finance
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase
from django.utils import timezone

from .anomalies import detect_after_hours, detect_round_number
from .audit import capture_diff, write_audit
from .models import (
    Budget,
    Department,
    FinanceCategory,
    FinancialAnomaly,
    FinancialAuditEntry,
    Loan,
    Payment,
    Transaction,
)
from .periods import Period, prior_period, resolve_period

User = get_user_model()


class PeriodResolutionTest(TestCase):
    def test_explicit_dates(self):
        p = resolve_period({'from': '2026-01-01', 'to': '2026-01-31'})
        self.assertEqual(p.start, date(2026, 1, 1))
        self.assertEqual(p.end, date(2026, 1, 31))
        self.assertEqual(p.days, 31)

    def test_mtd_preset(self):
        p = resolve_period({'preset': 'mtd'}, today=date(2026, 5, 15))
        self.assertEqual(p.start, date(2026, 5, 1))
        self.assertEqual(p.end, date(2026, 5, 15))

    def test_qtd_preset(self):
        p = resolve_period({'preset': 'qtd'}, today=date(2026, 5, 15))
        self.assertEqual(p.start, date(2026, 4, 1))  # Q2 starts April

    def test_last_year(self):
        p = resolve_period({'preset': 'last_year'}, today=date(2026, 5, 15))
        self.assertEqual(p.start, date(2025, 1, 1))
        self.assertEqual(p.end, date(2025, 12, 31))

    def test_prior_period_same_length(self):
        p = Period('test', date(2026, 5, 1), date(2026, 5, 31))
        prev = prior_period(p)
        self.assertEqual(prev.days, p.days)
        self.assertEqual(prev.end, date(2026, 4, 30))


class CaptureDiffTest(TestCase):
    def test_changed_field_only(self):
        class Stub:
            def __init__(self, **kw): self.__dict__.update(kw)
        old = Stub(name='Acme', total=Decimal('100.00'))
        new = Stub(name='Acme Ltd', total=Decimal('100.00'))
        diff = capture_diff(old, new, fields=['name', 'total'])
        self.assertEqual(diff, {'name': ['Acme', 'Acme Ltd']})
        # Decimal values that are equal must NOT appear in diff
        self.assertNotIn('total', diff)

    def test_decimal_serialization(self):
        class Stub:
            def __init__(self, **kw): self.__dict__.update(kw)
        old = Stub(total=Decimal('100.00'))
        new = Stub(total=Decimal('150.50'))
        diff = capture_diff(old, new, fields=['total'])
        # JSON-safe — both values stringified
        self.assertEqual(diff['total'], ['100.00', '150.50'])


class AnomalyRulesTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='auditor', password='x', email='a@example.com',
        )
        self.cat = FinanceCategory.objects.create(
            kind='revenue', code='SALES', name='Sales',
        )

    def _make_entry(self, *, amount=None, when=None) -> FinancialAuditEntry:
        return FinancialAuditEntry.objects.create(
            actor=self.user, actor_name='Auditor',
            action=FinancialAuditEntry.Action.UPDATE,
            model_name='Invoice', object_repr='INV-001',
            amount=amount, currency='GMD',
            timestamp=when or timezone.now(),
        )

    def test_round_number_creates_anomaly(self):
        e = self._make_entry(amount=Decimal('50000'))
        detect_round_number(e)
        self.assertEqual(
            FinancialAnomaly.objects.filter(
                kind=FinancialAnomaly.Kind.ROUND_NUMBER).count(),
            1,
        )

    def test_round_number_skips_small_amounts(self):
        e = self._make_entry(amount=Decimal('500'))
        detect_round_number(e)
        self.assertEqual(FinancialAnomaly.objects.count(), 0)

    def test_round_number_skips_non_round(self):
        e = self._make_entry(amount=Decimal('12345.67'))
        detect_round_number(e)
        self.assertEqual(FinancialAnomaly.objects.count(), 0)

    def test_after_hours_detection(self):
        # 23:00 local
        late = timezone.make_aware(
            timezone.datetime(2026, 5, 5, 23, 0),
        )
        e = self._make_entry(amount=Decimal('100'), when=late)
        detect_after_hours(e)
        self.assertEqual(
            FinancialAnomaly.objects.filter(
                kind=FinancialAnomaly.Kind.AFTER_HOURS_EDIT).count(),
            1,
        )


class WriteAuditTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='alice', password='x')
        self.cat = FinanceCategory.objects.create(
            kind='revenue', code='SALES', name='Sales',
        )

    def test_audit_writes_entry_with_request_metadata(self):
        rf = RequestFactory()
        req = rf.get('/x/', HTTP_USER_AGENT='TestAgent/1.0',
                     REMOTE_ADDR='10.0.0.5')
        # Need a saved instance
        tx = Transaction.objects.create(
            kind='revenue', source='manual',
            amount=Decimal('500'), currency='GMD',
            accrual_date=date.today(),
            category=self.cat, created_by=self.user,
        )
        entry = write_audit(
            action='create', instance=tx, actor=self.user,
            request=req, run_anomalies=False,
        )
        self.assertEqual(entry.actor, self.user)
        self.assertEqual(entry.actor_name, '')  # no full_name set
        self.assertEqual(entry.ip_address, '10.0.0.5')
        self.assertIn('TestAgent', entry.user_agent)
        self.assertEqual(entry.model_name, 'Ledger Entry')


class BudgetUtilizationTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='bob', password='x')
        self.dept = Department.objects.create(name='Ops', code='OPS')
        self.cat = FinanceCategory.objects.create(
            kind='expense', code='OTHER', name='Other Expenses',
        )
        self.budget = Budget.objects.create(
            department=self.dept, fiscal_year=2026,
            name='Ops 2026', total_amount=Decimal('10000'),
            starts_on=date(2026, 1, 1), ends_on=date(2026, 12, 31),
            created_by=self.user,
        )

    def test_no_spend(self):
        self.assertEqual(self.budget.spent, Decimal('0'))
        self.assertEqual(self.budget.utilization, 0)
        self.assertFalse(self.budget.over_budget)

    def test_partial_spend(self):
        Transaction.objects.create(
            kind='expense', source='manual',
            amount=Decimal('2500'), currency='GMD',
            accrual_date=date(2026, 3, 1),
            category=self.cat, department=self.dept,
            created_by=self.user,
        )
        self.assertEqual(self.budget.spent, Decimal('2500'))
        self.assertEqual(self.budget.utilization, 25.0)
        self.assertFalse(self.budget.over_budget)

    def test_over_budget(self):
        Transaction.objects.create(
            kind='expense', source='manual',
            amount=Decimal('12000'), currency='GMD',
            accrual_date=date(2026, 3, 1),
            category=self.cat, department=self.dept,
            created_by=self.user,
        )
        self.assertEqual(self.budget.utilization, 120.0)
        self.assertTrue(self.budget.over_budget)


class TransactionSignalsTest(TestCase):
    """Basic check that creating a Payment writes a Transaction + audit row."""

    def setUp(self):
        self.user = User.objects.create_user(username='cash', password='x')

    def test_payment_in_creates_transaction(self):
        p = Payment.objects.create(
            direction='in', method='bank',
            amount=Decimal('1000'), currency='GMD',
            paid_on=date.today(), counterparty='Acme',
            created_by=self.user,
        )
        # Signal handler should have run
        self.assertEqual(
            Transaction.objects.filter(source_payment=p,
                                        kind='payment_in').count(),
            1,
        )
        # And one audit entry
        self.assertGreaterEqual(
            FinancialAuditEntry.objects.filter(
                action=FinancialAuditEntry.Action.PAY).count(),
            1,
        )
