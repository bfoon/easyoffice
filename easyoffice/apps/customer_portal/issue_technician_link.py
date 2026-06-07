"""
apps/customer_portal/management/commands/issue_technician_link.py
=================================================================

Create (or show) the permanent portal link for a technician.

    python manage.py issue_technician_link --user jallow
    python manage.py issue_technician_link --email l.jallow@example.com
    python manage.py issue_technician_link --user jallow --revoke
    python manage.py issue_technician_link --user jallow --base-url https://ops.example.com
"""
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = 'Issue or show a technician portal link.'

    def add_arguments(self, parser):
        parser.add_argument('--user', help='Username of the technician.')
        parser.add_argument('--email', help='Email of the technician.')
        parser.add_argument('--base-url', default='', help='Base URL for the printed link.')
        parser.add_argument('--revoke', action='store_true', help='Revoke the existing link.')

    def handle(self, *args, **opts):
        from django.contrib.auth import get_user_model
        from apps.customer_portal.models_technician_portal import TechnicianPortalToken
        User = get_user_model()

        if not opts['user'] and not opts['email']:
            raise CommandError('Provide --user or --email.')

        user = None
        if opts['user']:
            user = User.objects.filter(username=opts['user']).first()
        if user is None and opts['email']:
            user = User.objects.filter(email__iexact=opts['email']).first()
        if user is None:
            raise CommandError('No matching user found.')

        tok, created = TechnicianPortalToken.objects.get_or_create(technician=user)

        if opts['revoke']:
            tok.revoke()
            self.stdout.write(self.style.WARNING(f'Revoked link for {user}.'))
            return

        base = opts['base_url'].rstrip('/')
        link = f'{base}/tech/{tok.token}/' if base else f'/tech/{tok.token}/'
        state = 'created' if created else 'existing'
        self.stdout.write(self.style.SUCCESS(f'[{state}] {user}: {link}'))
        if not tok.is_active:
            self.stdout.write(self.style.WARNING('  (token is currently revoked — re-run without --revoke to keep active)'))
