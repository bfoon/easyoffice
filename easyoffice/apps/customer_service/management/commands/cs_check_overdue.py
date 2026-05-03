"""
apps/customer_service/management/commands/cs_check_overdue.py
─────────────────────────────────────────────────────────────
Find tickets that are past their resolution_due_at and still open, and
notify the heads about each one (in-app + email).

Idempotency: each ticket is only flagged ONCE per breach. We mark it by
writing a ServiceTicketUpdate of type 'status_change' with a recognisable
body prefix; on subsequent runs we skip tickets that already have one.

Run on a cron / Celery beat:

    */15 * * * * cd /path/to/project && \\
        ./manage.py cs_check_overdue >> /var/log/django/cs_overdue.log 2>&1

Optional flags:
    --dry-run     Show what WOULD be notified, don't actually notify.
    --grace=N     Wait N minutes past due before flagging (default: 0).
"""
from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone

from apps.customer_service.models import ServiceTicket, ServiceTicketUpdate
from apps.customer_service import notifications


OVERDUE_FLAG_PREFIX = '[SLA-BREACH-NOTIFIED]'


class Command(BaseCommand):
    help = 'Notify heads about tickets that have breached their resolution SLA.'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true')
        parser.add_argument('--grace', type=int, default=0,
                            help='Minutes past due before flagging (default 0).')

    def handle(self, *args, **opts):
        now = timezone.now()
        threshold = now - timezone.timedelta(minutes=opts['grace'])

        # Tickets that are still open AND past due AND not previously flagged
        already_flagged_ids = (
            ServiceTicketUpdate.objects
            .filter(body__startswith=OVERDUE_FLAG_PREFIX)
            .values_list('ticket_id', flat=True)
        )

        qs = (
            ServiceTicket.objects
            .filter(resolution_due_at__lt=threshold)
            .exclude(status__in=('resolved', 'closed', 'cancelled'))
            .exclude(pk__in=already_flagged_ids)
            .select_related('customer', 'department', 'current_owner')
        )

        count = qs.count()
        self.stdout.write(f'Found {count} overdue ticket(s) to flag.')

        for ticket in qs:
            line = (
                f'{OVERDUE_FLAG_PREFIX} Ticket {ticket.ticket_no} flagged as overdue at '
                f'{now:%Y-%m-%d %H:%M}. Resolution was due '
                f'{ticket.resolution_due_at:%Y-%m-%d %H:%M}.'
            )
            if opts['dry_run']:
                self.stdout.write(self.style.WARNING(f'[dry-run] {line}'))
                continue

            ServiceTicketUpdate.objects.create(
                ticket=ticket, user=None, update_type='status_change', body=line,
            )
            try:
                notifications.notify_ticket_overdue(ticket)
            except Exception as exc:
                self.stderr.write(
                    self.style.ERROR(f'Notify failed for {ticket.ticket_no}: {exc}')
                )

        self.stdout.write(self.style.SUCCESS('Done.'))
