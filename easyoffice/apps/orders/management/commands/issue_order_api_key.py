"""
apps/orders/management/commands/issue_order_api_key.py
──────────────────────────────────────────────────────
Issue an API key for the orders endpoint.

Usage:
    ./manage.py issue_order_api_key --user website-bot@example.com --name "Website prod"

Prints the raw key to stdout. Save it; you cannot retrieve it later.
"""
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from apps.orders.api.models import ApiKey


class Command(BaseCommand):
    help = 'Issue an API key for the public orders endpoint.'

    def add_arguments(self, parser):
        parser.add_argument('--user', required=True,
                             help='Email or username of the user this key authenticates as.')
        parser.add_argument('--name', required=True,
                             help='Human label for the key (e.g. "Website production").')

    def handle(self, *args, **options):
        User = get_user_model()
        ident = options['user']
        user = User.objects.filter(email__iexact=ident).first() or \
               User.objects.filter(username__iexact=ident).first()
        if user is None:
            raise CommandError(f'No user matched "{ident}" (tried email and username).')

        api_key, raw = ApiKey.issue(name=options['name'], user=user)

        self.stdout.write(self.style.SUCCESS(
            f'\nIssued API key id={api_key.pk} ({api_key.name}) for {user}\n'
        ))
        self.stdout.write('Use this key in the Authorization header:\n\n')
        self.stdout.write(self.style.WARNING(f'  Authorization: Api-Key {raw}\n\n'))
        self.stdout.write(self.style.NOTICE(
            'This is the only time the key will be shown. Save it now.\n'
        ))
