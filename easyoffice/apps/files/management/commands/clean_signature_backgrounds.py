"""
apps/files/management/commands/clean_signature_backgrounds.py
─────────────────────────────────────────────────────────────
Re-process signatures that were saved before the transparency work.

Anything stored as a data:image with an opaque white canvas still carries
that canvas, so it keeps covering document text wherever it is stamped.
This command runs each one through apps.files.signature_image and writes
the cleaned PNG back.

    python manage.py clean_signature_backgrounds --dry-run
    python manage.py clean_signature_backgrounds
    python manage.py clean_signature_backgrounds --user baboucarrfoon@gmail.com

Typed signatures (``font:Name|Text``) and completed SignatureField values are
left alone: the first has no background, and the second is part of a signed
audit record that must not be rewritten after the fact.
"""
from __future__ import annotations

import base64

from django.core.management.base import BaseCommand

from apps.files.models import SavedSignature
from apps.files.signature_image import normalize_signature_data_url


class Command(BaseCommand):
    help = 'Make stored signature images transparent and tightly cropped.'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true',
                            help='Report what would change without saving.')
        parser.add_argument('--user', default=None,
                            help='Limit to one user (email or username).')
        parser.add_argument('--sensitivity', type=float, default=1.0,
                            help='0.5 strict … 1.8 keeps faint ink (default 1.0).')

    def handle(self, *args, **opts):
        qs = SavedSignature.objects.all()

        if opts['user']:
            qs = qs.filter(user__email__iexact=opts['user']) | \
                 qs.filter(user__username__iexact=opts['user'])

        changed = skipped = failed = 0

        for sig in qs.iterator():
            value = (sig.data or '').strip()

            if not value.startswith('data:image'):
                skipped += 1
                continue

            try:
                cleaned = normalize_signature_data_url(
                    value, sensitivity=opts['sensitivity'], keep_colour=True
                )
            except Exception as exc:                       # noqa: BLE001
                failed += 1
                self.stderr.write(f'  ! {sig.id} {sig.name}: {exc}')
                continue

            if cleaned == value:
                skipped += 1
                continue

            before = len(base64.b64decode(value.split(',', 1)[1]))
            after = len(base64.b64decode(cleaned.split(',', 1)[1]))
            self.stdout.write(
                f'  · {sig.user} / {sig.name}: {before // 1024} KB → {after // 1024} KB'
            )

            if not opts['dry_run']:
                sig.data = cleaned
                sig.save(update_fields=['data'])
            changed += 1

        verb = 'would be updated' if opts['dry_run'] else 'updated'
        self.stdout.write(self.style.SUCCESS(
            f'{changed} {verb}, {skipped} already clean or not an image, {failed} failed.'
        ))
