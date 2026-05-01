"""
manage.py issue_inventory_api_key --user <username-or-email> --name <label>

Prints the raw key once. After this, only the hash is stored — there is no
way to retrieve the key again. If lost, revoke and re-issue.
"""
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from apps.inventory.api.models import ApiKey


User = get_user_model()


class Command(BaseCommand):
    help = 'Issue a new API key for the inventory module.'

    def add_arguments(self, parser):
        parser.add_argument('--user', required=True,
                            help='Username or email of the key owner.')
        parser.add_argument('--name', required=True,
                            help='Human-readable label (e.g. "Warehouse scanner #1").')
        parser.add_argument('--expires-days', type=int, default=None,
                            help='Optional expiry in days from now.')

    def handle(self, *args, **opts):
        ident = opts['user']
        try:
            user = User.objects.get(username=ident)
        except User.DoesNotExist:
            user = User.objects.filter(email__iexact=ident).first()
            if not user:
                raise CommandError(f'No user matched "{ident}" (tried username, then email).')

        expires_at = None
        if days := opts.get('expires_days'):
            from django.utils import timezone
            from datetime import timedelta
            expires_at = timezone.now() + timedelta(days=int(days))

        record, raw = ApiKey.issue(
            user=user, name=opts['name'], expires_at=expires_at,
        )

        self.stdout.write(self.style.SUCCESS(
            '\n  Inventory API key issued — store this NOW; it cannot be retrieved later.\n'
        ))
        self.stdout.write(f'  Owner    : {user.get_username()} ({user.email or "no email"})')
        self.stdout.write(f'  Label    : {record.name}')
        self.stdout.write(f'  Prefix   : {record.lookup_prefix}')
        if expires_at:
            self.stdout.write(f'  Expires  : {expires_at.isoformat()}')
        self.stdout.write('')
        self.stdout.write(self.style.WARNING(f'  RAW KEY  : {raw}'))
        self.stdout.write('')
        self.stdout.write('  Use it as:  Authorization: Api-Key ' + raw)
        self.stdout.write('')
