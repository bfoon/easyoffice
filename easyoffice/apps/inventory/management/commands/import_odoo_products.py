"""
apps/inventory/management/commands/import_odoo_products.py
─────────────────────────────────────────────────────────
Bulk-import an Odoo "Product (product.template)" Excel export into the
EasyOffice inventory catalog.

Examples
════════
    # Dry-run first — see what WOULD happen, write nothing:
    ./manage.py import_odoo_products products.xlsx --dry-run

    # Real import, seed opening stock into the MAIN-WH location:
    ./manage.py import_odoo_products products.xlsx --location MAIN-WH

    # Catalog only (no stock movements), keep any real product images:
    ./manage.py import_odoo_products products.xlsx --no-stock --images

    # Attribute the import to a specific user (for created_by / movement actor):
    ./manage.py import_odoo_products products.xlsx --location WH-01 --user foon
"""
from __future__ import annotations

import json

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from apps.inventory.importers import import_odoo_products
from apps.inventory.models import Location

User = get_user_model()


class Command(BaseCommand):
    help = 'Import products from an Odoo product.template Excel export.'

    def add_arguments(self, parser):
        parser.add_argument('path', help='Path to the Odoo .xlsx export.')
        parser.add_argument(
            '--location', dest='location_code', default=None,
            help='Location CODE to receive opening stock into (e.g. MAIN-WH).',
        )
        parser.add_argument(
            '--user', dest='username', default=None,
            help='Username to attribute created_by / movement actor to.',
        )
        parser.add_argument(
            '--no-stock', dest='seed_stock', action='store_false',
            help="Import the catalog only — don't create opening-stock movements.",
        )
        parser.add_argument(
            '--images', dest='import_images', action='store_true',
            help='Import embedded product images (placeholders are always skipped).',
        )
        parser.add_argument(
            '--no-update', dest='update_existing', action='store_false',
            help='Skip rows whose product already exists instead of updating it.',
        )
        parser.add_argument(
            '--dry-run', dest='dry_run', action='store_true',
            help='Parse and validate but write nothing.',
        )
        parser.add_argument(
            '--json', dest='as_json', action='store_true',
            help='Emit the result as JSON instead of a human summary.',
        )

    def handle(self, *args, **opts):
        path = opts['path']

        location = None
        if opts['location_code']:
            try:
                location = Location.objects.get(code__iexact=opts['location_code'])
            except Location.DoesNotExist:
                raise CommandError(
                    f"No location with code '{opts['location_code']}'. "
                    f"Available: {', '.join(Location.objects.values_list('code', flat=True)) or '(none)'}"
                )

        if opts['seed_stock'] and location is None and not opts['dry_run']:
            raise CommandError(
                'Opening stock needs a destination. Pass --location CODE, '
                'or --no-stock to import the catalog only.'
            )

        actor = None
        if opts['username']:
            try:
                actor = User.objects.get(username=opts['username'])
            except User.DoesNotExist:
                raise CommandError(f"No user named '{opts['username']}'.")

        try:
            result = import_odoo_products(
                path,
                location=location,
                actor=actor,
                seed_stock=opts['seed_stock'],
                import_images=opts['import_images'],
                update_existing=opts['update_existing'],
                dry_run=opts['dry_run'],
            )
        except Exception as exc:  # noqa: BLE001
            raise CommandError(str(exc))

        if opts['as_json']:
            self.stdout.write(json.dumps(result.as_dict(), indent=2))
            return

        d = result.as_dict()
        tag = '[DRY-RUN] ' if d['dry_run'] else ''
        self.stdout.write(self.style.MIGRATE_HEADING(f'\n{tag}Odoo import summary'))
        self.stdout.write(f"  Rows read:        {d['total_rows']}")
        self.stdout.write(self.style.SUCCESS(f"  Created:          {d['created']}"))
        self.stdout.write(f"  Updated:          {d['updated']}")
        self.stdout.write(f"  Skipped:          {d['skipped']}")
        self.stdout.write(f"  Categories made:  {d['categories_made']}")
        self.stdout.write(f"  Stock seeded:     {d['stock_seeded']}")
        self.stdout.write(f"  Images imported:  {d['images_imported']}")

        if d['issues']:
            self.stdout.write(self.style.WARNING(f"\n  {len(d['issues'])} issue(s):"))
            for i in d['issues'][:50]:
                self.stdout.write(f"    row {i['row']}: {i['name']} — {i['message']}")
            if len(d['issues']) > 50:
                self.stdout.write(f"    … and {len(d['issues']) - 50} more.")

        self.stdout.write('')
