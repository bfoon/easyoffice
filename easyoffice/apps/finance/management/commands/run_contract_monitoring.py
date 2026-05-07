from decimal import Decimal
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.core.mail import send_mail
from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from apps.core.models import User
from apps.finance.models import (
    Contract,
    ContractAlertLog,
    ContractInvoiceLink,
    IncomingPaymentRequest,
)
from apps.finance.views import _send_invoice_email


def _contract_role_recipients():
    """
    Finance + HR + CEO + Admin + superusers
    """
    return User.objects.filter(
        is_active=True
    ).filter(
        Q(groups__name__in=['Finance', 'HR', 'CEO', 'Admin']) | Q(is_superuser=True)
    ).distinct()


def _send_contract_email(subject, message, recipients):
    recipients = [r for r in recipients if r]
    if not recipients:
        return 0

    try:
        send_mail(
            subject=subject,
            message=message,
            from_email=getattr(settings, 'DEFAULT_FROM_EMAIL', 'noreply@example.com'),
            recipient_list=recipients,
            fail_silently=False,
        )
        return len(recipients)
    except Exception:
        return 0


def _already_logged_today(contract, alert_type, today):
    return ContractAlertLog.objects.filter(
        contract=contract,
        alert_type=alert_type,
        sent_at__date=today,
    ).exists()


def _next_invoice_date_from_cycle(base_date, cycle):
    if cycle == Contract.BillingCycle.WEEKLY:
        return base_date + timedelta(days=7)
    if cycle == Contract.BillingCycle.MONTHLY:
        return base_date + timedelta(days=30)
    if cycle == Contract.BillingCycle.QUARTERLY:
        return base_date + timedelta(days=90)
    if cycle == Contract.BillingCycle.YEARLY:
        return base_date + timedelta(days=365)
    return None


def _create_invoice_from_contract(contract, send_now=False):
    """
    Auto-create an invoice from a vendor contract.
    """
    if contract.contract_type != Contract.ContractType.VENDOR:
        return None

    customer_name = contract.vendor_name or contract.vendor_company or 'Customer'
    customer_email = contract.vendor_email or ''

    ipr = IncomingPaymentRequest.objects.create(
        title=contract.title,
        description=contract.description or f'Contract billing for {contract.title}',
        customer_name=customer_name,
        customer_email=customer_email,
        customer_phone=contract.vendor_phone or '',
        customer_company=contract.vendor_company or '',
        customer_address=contract.vendor_address or '',
        amount=contract.standard_cost or Decimal('0'),
        tax_amount=Decimal('0'),
        discount_amount=Decimal('0'),
        currency=contract.currency or 'GMD',
        issue_date=timezone.now().date(),
        due_date=timezone.now().date() + timedelta(days=14),
        payment_instructions='Please settle according to agreed contract terms.',
        notes=f'Auto-generated from contract {contract.reference or contract.pk}',
        project=contract.project,
        budget=contract.budget,
        status=IncomingPaymentRequest.Status.DRAFT,
        created_by=contract.created_by or User.objects.filter(is_superuser=True).first(),
    )

    ContractInvoiceLink.objects.create(
        contract=contract,
        invoice=ipr,
        period_start=contract.last_invoice_date or contract.start_date,
        period_end=timezone.now().date(),
    )

    today = timezone.now().date()
    contract.last_invoice_date = today
    contract.next_invoice_date = _next_invoice_date_from_cycle(today, contract.billing_cycle)
    contract.save(update_fields=['last_invoice_date', 'next_invoice_date', 'updated_at'])

    if send_now and ipr.customer_email:
        ipr.status = IncomingPaymentRequest.Status.SENT
        ipr.save(update_fields=['status', 'updated_at'])
        _send_invoice_email(ipr, request=None)

    return ipr


def run_contract_monitoring(stdout=None, dry_run=False):
    today = timezone.now().date()

    contracts = Contract.objects.select_related(
        'staff', 'project', 'budget', 'created_by'
    ).all()

    role_users = list(_contract_role_recipients())

    results = {
        'contracts_checked': 0,
        'status_updated': 0,
        'expiry_alerts_sent': 0,
        'renewal_alerts_sent': 0,
        'invoices_created': 0,
        'invoices_emailed': 0,
    }

    for contract in contracts:
        results['contracts_checked'] += 1

        old_status = contract.status
        contract.update_status_by_dates()
        if contract.status != old_status:
            results['status_updated'] += 1
            if not dry_run:
                contract.save(update_fields=['status', 'updated_at'])

        # ── Expiry alerts ─────────────────────────────────────────────
        if (
            contract.send_expiry_alerts
            and contract.days_to_end >= 0
            and contract.days_to_end <= contract.alert_days_before_end
            and not _already_logged_today(contract, ContractAlertLog.AlertType.EXPIRY, today)
        ):
            recipients = [u.email for u in role_users if u.email]
            if contract.counterparty_email:
                recipients.append(contract.counterparty_email)

            recipients = sorted(set([r for r in recipients if r]))

            subject = f'Contract expiring soon: {contract.title}'
            message = (
                f'Contract: {contract.title}\n'
                f'Type: {contract.get_contract_type_display()}\n'
                f'Counterparty: {contract.counterparty_name}\n'
                f'End date: {contract.end_date}\n'
                f'Days remaining: {contract.days_to_end}\n'
                f'Reference: {contract.reference or contract.pk}\n'
            )

            if dry_run:
                sent_count = len(recipients)
            else:
                sent_count = _send_contract_email(subject, message, recipients)
                ContractAlertLog.objects.create(
                    contract=contract,
                    alert_type=ContractAlertLog.AlertType.EXPIRY,
                    sent_to=', '.join(recipients),
                    message=message,
                    sent_by=contract.updated_by or contract.created_by,
                )

            if sent_count:
                results['expiry_alerts_sent'] += 1
                if stdout:
                    stdout.write(f'[EXPIRY] {contract.title} -> {", ".join(recipients)}')

        # ── Renewal alerts ───────────────────────────────────────────
        if (
            contract.send_renewal_alerts
            and contract.renewal_date
            and contract.renewal_date >= today
            and (contract.renewal_date - today).days <= contract.alert_days_before_renewal
            and not _already_logged_today(contract, ContractAlertLog.AlertType.RENEWAL, today)
        ):
            recipients = [u.email for u in role_users if u.email]
            if contract.counterparty_email:
                recipients.append(contract.counterparty_email)

            recipients = sorted(set([r for r in recipients if r]))

            subject = f'Contract renewal due soon: {contract.title}'
            message = (
                f'Contract: {contract.title}\n'
                f'Type: {contract.get_contract_type_display()}\n'
                f'Counterparty: {contract.counterparty_name}\n'
                f'Renewal date: {contract.renewal_date}\n'
                f'Days remaining: {(contract.renewal_date - today).days}\n'
                f'Reference: {contract.reference or contract.pk}\n'
            )

            if dry_run:
                sent_count = len(recipients)
            else:
                sent_count = _send_contract_email(subject, message, recipients)
                ContractAlertLog.objects.create(
                    contract=contract,
                    alert_type=ContractAlertLog.AlertType.RENEWAL,
                    sent_to=', '.join(recipients),
                    message=message,
                    sent_by=contract.updated_by or contract.created_by,
                )

            if sent_count:
                results['renewal_alerts_sent'] += 1
                if stdout:
                    stdout.write(f'[RENEWAL] {contract.title} -> {", ".join(recipients)}')

        # ── Auto invoice generation ──────────────────────────────────
        if (
            contract.auto_generate_invoice
            and contract.contract_type == Contract.ContractType.VENDOR
            and contract.next_invoice_date
            and contract.next_invoice_date <= today
            and contract.status not in [Contract.Status.EXPIRED, Contract.Status.TERMINATED]
        ):
            already_has_invoice_today = ContractInvoiceLink.objects.filter(
                contract=contract,
                created_at__date=today,
            ).exists()

            if not already_has_invoice_today:
                if dry_run:
                    results['invoices_created'] += 1
                    if contract.auto_send_invoice and contract.counterparty_email:
                        results['invoices_emailed'] += 1
                    if stdout:
                        stdout.write(f'[INVOICE] Would create invoice for {contract.title}')
                else:
                    ipr = _create_invoice_from_contract(
                        contract,
                        send_now=contract.auto_send_invoice
                    )
                    if ipr:
                        results['invoices_created'] += 1
                        if contract.auto_send_invoice and contract.counterparty_email:
                            results['invoices_emailed'] += 1

                        ContractAlertLog.objects.create(
                            contract=contract,
                            alert_type=ContractAlertLog.AlertType.INVOICE,
                            sent_to=contract.counterparty_email or '',
                            message=f'Invoice {ipr.invoice_number} created from contract.',
                            sent_by=contract.updated_by or contract.created_by,
                        )

                        if stdout:
                            stdout.write(
                                f'[INVOICE] {contract.title} -> {ipr.invoice_number}'
                                + (' (emailed)' if contract.auto_send_invoice and contract.counterparty_email else '')
                            )

    return results


class Command(BaseCommand):
    help = 'Run contract monitoring: update status, send expiry/renewal alerts, and auto-generate invoices.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Show what would happen without sending emails or creating invoices.'
        )

    def handle(self, *args, **options):
        dry_run = options.get('dry_run', False)

        self.stdout.write(self.style.NOTICE(
            f'Running contract monitoring{" (dry-run)" if dry_run else ""}...'
        ))

        results = run_contract_monitoring(stdout=self.stdout, dry_run=dry_run)

        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS('Contract monitoring finished.'))
        self.stdout.write(f"Contracts checked: {results['contracts_checked']}")
        self.stdout.write(f"Statuses updated: {results['status_updated']}")
        self.stdout.write(f"Expiry alerts sent: {results['expiry_alerts_sent']}")
        self.stdout.write(f"Renewal alerts sent: {results['renewal_alerts_sent']}")
        self.stdout.write(f"Invoices created: {results['invoices_created']}")
        self.stdout.write(f"Invoices emailed: {results['invoices_emailed']}")