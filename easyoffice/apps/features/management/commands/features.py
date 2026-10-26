"""
Emergency / scripting access to feature flags.

    python manage.py features list
    python manage.py features off pos
    python manage.py features on investments
    python manage.py features off inventory --cascade
    python manage.py features reset          # delete all rows → defaults
"""
from django.core.management.base import BaseCommand, CommandError

from apps.features import services as fs
from apps.features.registry import registry


class Command(BaseCommand):
    help = 'List or change EasyOffice feature flags.'

    def add_arguments(self, parser):
        parser.add_argument('action', choices=['list', 'on', 'off', 'pilot', 'reset'])
        parser.add_argument('key', nargs='?')
        parser.add_argument('--cascade', action='store_true')

    def handle(self, *args, **opts):
        action, key = opts['action'], opts.get('key')
        if action == 'list':
            for category, specs in registry.by_category():
                self.stdout.write(self.style.MIGRATE_HEADING(category))
                for s in specs:
                    mode = 'locked' if s.locked else fs.mode_for(s.key)
                    req = f'  (requires {", ".join(s.requires)})' if s.requires else ''
                    self.stdout.write(f'  {s.key:<20} {mode:<7} {s.name}{req}')
            return
        if action == 'reset':
            from apps.features.models import FeatureFlag
            n, _ = FeatureFlag.objects.all().delete()
            fs.invalidate_cache()
            self.stdout.write(self.style.SUCCESS(f'Removed {n} flag rows; all features at defaults.'))
            return
        if not key:
            raise CommandError('Feature key required.')
        try:
            res = fs.set_mode(key, action, cascade=opts['cascade'], note='manage.py features')
        except fs.FeatureDependencyError as exc:
            raise CommandError(f'{exc}  (use --cascade)')
        except ValueError as exc:
            raise CommandError(str(exc))
        self.stdout.write(self.style.SUCCESS(f'{key} → {action}'))
        for s in res['auto_enabled']:
            self.stdout.write(f'  also on: {s.key}')
        for s in res['cascaded']:
            self.stdout.write(f'  also off: {s.key}')
