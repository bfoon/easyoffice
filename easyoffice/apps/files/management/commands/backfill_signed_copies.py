"""
apps/files/management/commands/backfill_signed_copies.py
────────────────────────────────────────────────────────
Repair access on signature requests that completed before signed-copy
delivery existed.

Those requests produced a signed PDF owned by the original document owner
with no share rows, so every other signer hit "page not found" when they came
back to download it. This grants each participant view access on the signed
file.

    python manage.py backfill_signed_copies --dry-run
    python manage.py backfill_signed_copies                  # access only
    python manage.py backfill_signed_copies --notify         # + message them

``--notify`` is off by default on purpose: running it over a year of history
would fire a chat message and a notification for every old document at once.
Use it only for a narrow ``--since`` window, or for one request with ``--id``.
"""
from __future__ import annotations

from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.files.models import SignatureRequest
from apps.files.signature_delivery import deliver_signed_copies


class Command(BaseCommand):
    help = 'Grant participants access to signed copies on completed requests.'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true',
                            help='List what would change without writing.')
        parser.add_argument('--notify', action='store_true',
                            help='Also send the in-app message and chat copy.')
        parser.add_argument('--id', default=None,
                            help='Limit to a single signature request id.')
        parser.add_argument('--since', type=int, default=None,
                            help='Only requests completed in the last N days.')

    def handle(self, *args, **opts):
        qs = SignatureRequest.objects.filter(status='completed')

        if opts['id']:
            qs = qs.filter(pk=opts['id'])
        if opts['since']:
            qs = qs.filter(
                completed_at__gte=timezone.now() - timedelta(days=opts['since'])
            )

        qs = qs.select_related('document', 'created_by').order_by('completed_at')

        total = granted = notified = 0

        for sig_req in qs.iterator():
            total += 1

            if opts['dry_run']:
                participants = sig_req.signers.exclude(user=None).count()
                self.stdout.write(
                    f'  · {sig_req.title[:52]} — {participants} participant(s)'
                )
                continue

            result = deliver_signed_copies(sig_req, notify=opts['notify'])
            granted += result.get('granted', 0)
            notified += result.get('notified', 0)

            self.stdout.write(
                f'  · {sig_req.title[:52]} — '
                f"{result.get('granted', 0)} granted, "
                f"{result.get('notified', 0)} notified"
            )

        if opts['dry_run']:
            self.stdout.write(self.style.WARNING(
                f'{total} completed request(s) would be processed. '
                f'Re-run without --dry-run to apply.'
            ))
        else:
            self.stdout.write(self.style.SUCCESS(
                f'{total} request(s): {granted} access grant(s), '
                f'{notified} notification(s).'
            ))
