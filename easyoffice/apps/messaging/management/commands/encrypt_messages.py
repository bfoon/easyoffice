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

IMPORTANT
---------
This command MUST bypass:
  1. The post_init signal (which would auto-decrypt content on load).
  2. The save() override in EncryptedContentMixin (which would re-encrypt).

It does this by:
  1. Reading raw DB values with .values_list("id", "content") — no model
     instantiation, no post_init signal fires.
  2. Writing with ChatMessage.objects.filter(pk=...).update(content=...) —
     goes straight to SQL UPDATE, never calls save().
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

        total = ChatMessage.objects.count()
        self.stdout.write(f"Total messages: {total}")
        if dry_run:
            self.stdout.write(self.style.WARNING("DRY RUN – no writes will occur."))

        processed = 0
        skipped   = 0
        errors    = 0

        # ───────────────────────────────────────────────────────────────
        # Read RAW content via values_list — bypasses post_init signal.
        # ───────────────────────────────────────────────────────────────
        qs = (
            ChatMessage.objects
            .values_list("id", "content")
            .iterator(chunk_size=batch_size)
        )

        batch = []   # list of (id, new_encrypted_content) tuples

        def flush(pending):
            """Write a batch via .filter().update() — bypasses save() override."""
            nonlocal errors
            if not pending:
                return 0
            if dry_run:
                return len(pending)
            try:
                with transaction.atomic():
                    for row_id, new_content in pending:
                        ChatMessage.objects.filter(pk=row_id).update(content=new_content)
                return len(pending)
            except Exception:
                logger.exception("Bulk update failed for a batch")
                errors += len(pending)
                return 0

        for row_id, raw in qs:
            raw = raw or ""
            if not raw or is_encrypted(raw):
                skipped += 1
                continue

            try:
                new_content = encrypt_content(raw)
            except Exception:
                logger.exception("Encryption failed for message %s", row_id)
                errors += 1
                continue

            batch.append((row_id, new_content))

            if len(batch) >= batch_size:
                flushed = flush(batch)
                processed += flushed
                batch = []
                self.stdout.write(f"  … encrypted {processed} so far")

        # final partial batch
        if batch:
            flushed = flush(batch)
            processed += flushed

        self.stdout.write(
            self.style.SUCCESS(
                f"\nDone. encrypted={processed}, "
                f"skipped(already enc or empty)={skipped}, errors={errors}"
            )
        )