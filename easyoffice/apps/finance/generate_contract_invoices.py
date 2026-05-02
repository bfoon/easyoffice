"""
apps/finance/management/commands/generate_contract_invoices.py
==============================================================

Runs daily (via cron) to generate invoices for contracts whose
`next_invoice_date` is on or before today.

Usage:
    python manage.py generate_contract_invoices            # run for today
    python manage.py generate_contract_invoices --date 2026-05-01
    python manage.py generate_contract_invoices --dry-run

Suggested cron entry (daily at 6am):
    0 6 * * *  cd /path/to/project && /path/to/venv/bin/python manage.py generate_contract_invoices
"""
from datetime import datetime
from django.core.management.base import BaseCommand, CommandError

from apps.finance.contract_invoice_service import (
    run_scheduled_generation, find_contracts_due,
)


class Command(BaseCommand):
    help = 'Generate invoices for all contracts whose next_invoice_date is due.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--date',
            type=str,
            default=None,
            help='Override the run date (YYYY-MM-DD). Defaults to today.',
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='List contracts that would be invoiced, but don\'t generate anything.',
        )

    def handle(self, *args, **opts):
        run_date = None
        if opts['date']:
            try:
                run_date = datetime.strptime(opts['date'], '%Y-%m-%d').date()
            except ValueError:
                raise CommandError('--date must be in YYYY-MM-DD format')

        if opts['dry_run']:
            due = list(find_contracts_due(run_date))
            self.stdout.write(self.style.NOTICE(
                f'[DRY RUN] {len(due)} contract(s) would be invoiced:'
            ))
            for c in due:
                self.stdout.write(
                    f'  • {c.title}  (next_invoice_date={c.next_invoice_date}, '
                    f'cycle={c.billing_cycle}, cost={c.currency} {c.standard_cost})'
                )
            return

        summary = run_scheduled_generation(today=run_date)

        self.stdout.write(self.style.SUCCESS(
            f"Run for {summary['date']}: "
            f"attempted={summary['attempted']}, "
            f"succeeded={summary['succeeded']}, "
            f"failed={summary['failed']}"
        ))
        for cid, number in summary['invoices']:
            self.stdout.write(f'  ✓ {cid}: {number}')
        for cid, err in summary['errors']:
            self.stdout.write(self.style.WARNING(f'  ✗ {cid}: {err}'))
