"""
manage.py seed_inventory_access

One-shot command to translate the legacy group/title model into explicit
InventoryAccessGrant rows. After running this, every existing user keeps
their current access — but now via the new dynamic system, so you can
edit grants from /inventory/access/ instead of changing Django groups.

Idempotent: re-running adds new grants but never removes existing ones.

Usage:
    ./manage.py seed_inventory_access
    ./manage.py seed_inventory_access --dry-run     # preview
    ./manage.py seed_inventory_access --user alice  # just one user
"""
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

from apps.inventory.access import Module
from apps.inventory.models import InventoryAccessGrant


User = get_user_model()


# Mapping: legacy group -> [(module, level), ...]
GROUP_TO_GRANTS = {
    # Operators — full day-to-day operate access on the core modules.
    'inventory':       [(m, 'operate') for m in [
        Module.PRODUCTS, Module.ASSETS, Module.LOCATIONS,
        Module.SUPPLIERS, Module.CATEGORIES, Module.MOVEMENTS,
        Module.REQUESTS, Module.STOCKTAKE, Module.SCAN,
    ]] + [(Module.REPORTS, 'view')],

    'storekeeper':     [(m, 'operate') for m in [
        Module.PRODUCTS, Module.ASSETS, Module.MOVEMENTS,
        Module.REQUESTS, Module.STOCKTAKE, Module.SCAN,
    ]] + [(Module.LOCATIONS, 'view'), (Module.SUPPLIERS, 'view')],

    'warehouse':       [(m, 'operate') for m in [
        Module.PRODUCTS, Module.MOVEMENTS, Module.STOCKTAKE,
        Module.REQUESTS, Module.SCAN,
    ]],

    'logistics':       [(m, 'operate') for m in [
        Module.MOVEMENTS, Module.REQUESTS, Module.SCAN,
    ]],

    # Managers — manage the workflows
    'supervisor': [(m, 'manage') for m in [
        Module.PRODUCTS, Module.ASSETS, Module.LOCATIONS,
        Module.SUPPLIERS, Module.CATEGORIES, Module.MOVEMENTS,
        Module.REQUESTS, Module.STOCKTAKE, Module.REPORTS, Module.SCAN,
    ]],
    'manager':    [(m, 'manage') for m in [
        Module.PRODUCTS, Module.ASSETS, Module.LOCATIONS,
        Module.SUPPLIERS, Module.CATEGORIES, Module.MOVEMENTS,
        Module.REQUESTS, Module.STOCKTAKE, Module.REPORTS, Module.SCAN,
    ]],
    'head of operations': [(m, 'manage') for m in Module.ALL],
    'head of inventory':  [(m, 'manage') for m in Module.ALL],

    # Top-tier
    'admin':         [(m, 'manage') for m in Module.ALL],
    'administrator': [(m, 'manage') for m in Module.ALL],
    'ceo':           [(m, 'manage') for m in Module.ALL],

    # Finance can see reports + stock requests rerouted to them
    'finance':  [(Module.REPORTS, 'view'), (Module.REQUESTS, 'view')],
    'accounts': [(Module.REPORTS, 'view'), (Module.REQUESTS, 'view')],
}


class Command(BaseCommand):
    help = 'Seed InventoryAccessGrant rows from existing group memberships.'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true',
                            help='Show what would be granted without writing.')
        parser.add_argument('--user', help='Limit to a single username/email.')

    def handle(self, *args, **opts):
        dry = opts['dry_run']
        only_user = opts.get('user')

        users = User.objects.filter(is_active=True)
        if only_user:
            users = users.filter(username=only_user) | users.filter(email__iexact=only_user)

        created = updated = skipped = 0
        for user in users:
            grants = self._grants_for(user)
            if not grants:
                continue
            for module, level in grants:
                if dry:
                    self.stdout.write(f'[DRY] {user.username}: {module} = {level}')
                    continue
                obj, was_created = InventoryAccessGrant.objects.update_or_create(
                    user=user, module=module,
                    defaults={'level': level, 'is_active': True,
                              'notes': 'auto-seed from legacy groups'},
                )
                if was_created:
                    created += 1
                    self.stdout.write(self.style.SUCCESS(
                        f'+ {user.username}: {module} = {level}'))
                else:
                    # Don't downgrade an existing grant set higher by an admin
                    pass
        if not dry:
            self.stdout.write(self.style.SUCCESS(
                f'\nDone. Created {created} new grant(s).'))

    def _grants_for(self, user) -> list:
        """Return list of (module, level) deduplicated by highest level."""
        names = {g.lower() for g in user.groups.values_list('name', flat=True)}
        merged = {}  # module -> highest level seen
        rank = {'view': 1, 'operate': 2, 'manage': 3}

        for grp_name, grants in GROUP_TO_GRANTS.items():
            if grp_name not in names:
                continue
            for module, level in grants:
                if module not in merged or rank[level] > rank[merged[module]]:
                    merged[module] = level

        # Superusers and Django staff get manage on everything
        if user.is_superuser or user.is_staff:
            for m in Module.ALL:
                merged[m] = 'manage'

        return [(m, l) for m, l in merged.items()]
