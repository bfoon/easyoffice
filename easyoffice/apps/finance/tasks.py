"""
apps/finance/tasks.py
======================

Celery tasks for contract invoice automation + maintenance roster scheduling.

Three tasks
-----------
1. generate_due_contract_invoices
   Runs every morning (e.g. 6 am).  Picks all active contracts whose
   next_invoice_date <= today, generates + finalizes an InvoiceDocument for
   each, and advances next_invoice_date.  Does NOT send emails.

2. send_due_contract_invoice_emails
   Runs every morning (e.g. 6:30 am, AFTER generation).  Picks every
   ContractInvoiceLink whose invoice.due_date == today and whose contract
   has auto_send_invoice=True, and emails the PDF to the counterparty.

3. schedule_due_maintenance_visits
   Runs every morning (e.g. 6:15 am).  Walks every active MaintenanceRoster
   whose next visit falls inside its notice window, materializes the visit,
   creates ONE shared Task (lead = assignee, other responsible parties =
   collaborators), and emails calendar invites (.ics) containing the public
   completion link.  Advances next_visit_date.

Add all three to your Celery Beat schedule:

    from celery.schedules import crontab

    CELERY_BEAT_SCHEDULE = {
        # Step 1 — generate invoices for all due contracts
        'generate-contract-invoices-daily': {
            'task': 'apps.finance.tasks.generate_due_contract_invoices',
            'schedule': crontab(hour=6, minute=0),
        },
        # Step 2 — materialize maintenance visits + send calendar invites
        'schedule-maintenance-visits-daily': {
            'task': 'apps.finance.tasks.schedule_due_maintenance_visits',
            'schedule': crontab(hour=6, minute=15),
        },
        # Step 3 — send invoices whose due_date is today
        'send-due-contract-invoice-emails-daily': {
            'task': 'apps.finance.tasks.send_due_contract_invoice_emails',
            'schedule': crontab(hour=6, minute=30),
        },
    }
"""
import logging
from celery import shared_task

from apps.finance.contract_invoice_service import (
    run_scheduled_generation,
    send_due_invoice_emails,
)
from apps.finance.contract_maintenance_service import (
    run_scheduled_maintenance,
)

logger = logging.getLogger(__name__)


# ── Task 1: generate invoices ────────────────────────────────────────────────

@shared_task(
    name='apps.finance.tasks.generate_due_contract_invoices',
    bind=True,
    max_retries=2,
    default_retry_delay=60 * 10,   # 10 min between retries
)
def generate_due_contract_invoices(self):
    """
    Generate invoices for every contract whose next_invoice_date is due.
    Idempotent per period (next_invoice_date is advanced after each success).
    """
    try:
        summary = run_scheduled_generation()
    except Exception as exc:
        logger.exception('Scheduled contract invoice generation crashed')
        raise self.retry(exc=exc)

    logger.info(
        'Contract invoice generation: %s attempted, %s succeeded, %s failed',
        summary['attempted'], summary['succeeded'], summary['failed'],
    )
    for cid, number in summary['invoices']:
        logger.info('  Generated %s for contract %s', number, cid)
    for cid, err in summary['errors']:
        logger.warning('  Contract %s: %s', cid, err)

    return summary


# ── Task 2: send invoices whose due_date is today ────────────────────────────

@shared_task(
    name='apps.finance.tasks.send_due_contract_invoice_emails',
    bind=True,
    max_retries=2,
    default_retry_delay=60 * 10,
)
def send_due_contract_invoice_emails(self):
    """
    Email PDF invoices to counterparties for every ContractInvoiceLink whose
    invoice.due_date == today and whose contract has auto_send_invoice=True.

    This task runs AFTER generate_due_contract_invoices so any invoices
    generated today (with due_date = today) are also caught.
    """
    try:
        summary = send_due_invoice_emails()
    except Exception as exc:
        logger.exception('Due invoice email sending crashed')
        raise self.retry(exc=exc)

    logger.info(
        'Contract invoice emails: %s attempted, %s sent, %s failed',
        summary['attempted'], summary['sent'], summary['failed'],
    )
    for inv_id, err in summary['errors']:
        logger.warning('  Invoice %s: %s', inv_id, err)

    return summary


# ── Task 3: schedule maintenance visits + send calendar invites ──────────────

@shared_task(
    name='apps.finance.tasks.schedule_due_maintenance_visits',
    bind=True,
    max_retries=2,
    default_retry_delay=60 * 10,
)
def schedule_due_maintenance_visits(self):
    """
    Materialize maintenance visits for every active roster whose next visit
    falls inside its notice window. For each visit: creates the shared Task
    (all responsible parties as collaborators) and emails calendar invites
    with the public completion link. Idempotent per (roster, date).
    """
    try:
        summary = run_scheduled_maintenance()
    except Exception as exc:
        logger.exception('Scheduled maintenance visit creation crashed')
        raise self.retry(exc=exc)

    logger.info(
        'Maintenance scheduling: %s attempted, %s succeeded, %s failed',
        summary['attempted'], summary['succeeded'], summary['failed'],
    )
    for rid, vdate in summary['visits']:
        logger.info('  Scheduled visit %s for roster %s', vdate, rid)
    for rid, err in summary['errors']:
        logger.warning('  Roster %s: %s', rid, err)

    return summary