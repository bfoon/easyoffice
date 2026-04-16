"""
apps/messaging/management/commands/encrypt_messages.py
────────────────────────────────────────────────────────
One-shot management command that encrypts every existing ChatMessage that
still has plain-text content (i.e. content that does NOT start with "enc:").

Run after deploying the EncryptedContentMixin to back-fill historical rows:

    python manage.py encrypt_messages

Options
-------
--batch-size  Number of rows to process per DB transaction (default 500).
--dry-run     Print statistics without writing anything to the database.

The command is idempotent – running it multiple times is safe.
"""

import logging

from django.core.management.base import BaseCommand
from django.db import transaction

from apps.messaging.encryption import encrypt_content, is_encrypted
from apps.messaging.models import ChatMessage

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Encrypt all plain-text ChatMessage.content values at rest."

    def add_arguments(self, parser):
        parser.add_argument(
            "--batch-size",
            type=int,
            default=500,
            help="Rows per transaction (default: 500)",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Count rows that need encrypting without writing anything.",
        )

    def handle(self, *args, **options):
        batch_size = options["batch_size"]
        dry_run    = options["dry_run"]

        total     = ChatMessage.objects.count()
        processed = 0
        skipped   = 0
        errors    = 0

        self.stdout.write(f"Total messages: {total}")
        if dry_run:
            self.stdout.write(self.style.WARNING("DRY RUN – no writes will occur."))

        qs = (
            ChatMessage.objects
            .only("id", "content")
            .iterator(chunk_size=batch_size)
        )

        batch = []
        for msg in qs:
            raw = msg.content or ""
            if not raw or is_encrypted(raw):
                skipped += 1
                continue

            msg.content = encrypt_content(raw)
            batch.append(msg)

            if len(batch) >= batch_size:
                if not dry_run:
                    try:
                        with transaction.atomic():
                            ChatMessage.objects.bulk_update(batch, ["content"])
                    except Exception:
                        logger.exception("Bulk update failed for a batch")
                        errors += len(batch)
                processed += len(batch)
                batch = []
                self.stdout.write(f"  … encrypted {processed} so far")

        # final partial batch
        if batch:
            if not dry_run:
                try:
                    with transaction.atomic():
                        ChatMessage.objects.bulk_update(batch, ["content"])
                except Exception:
                    logger.exception("Bulk update failed for final batch")
                    errors += len(batch)
            processed += len(batch)

        self.stdout.write(
            self.style.SUCCESS(
                f"\nDone. encrypted={processed}, skipped(already enc)={skipped}, errors={errors}"
            )
        )
