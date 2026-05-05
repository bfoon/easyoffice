from django.apps import AppConfig


class FinanceConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.finance'
    verbose_name = 'Finance'

    def ready(self):
        # Wire up audit + anomaly signal handlers as soon as the app loads.
        # Importing the module is enough — the @receiver decorators do the rest.
        from apps.finance import signals  # noqa: F401
