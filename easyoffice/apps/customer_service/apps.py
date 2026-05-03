from django.apps import AppConfig


class CustomerServiceConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.customer_service'
    verbose_name = 'Customer Service'

    def ready(self):
        # Importing the module is enough — the @receiver decorators register
        # the handlers as a side effect. Wrap in try/except so a missing
        # signals module never breaks app boot.
        try:
            from . import signals  # noqa: F401
        except Exception:
            import logging
            logging.getLogger(__name__).exception(
                'customer_service: failed to load signals module'
            )
