from django.apps import AppConfig


class AdminSecurityConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.admin_security'
    verbose_name = 'Admin Security'

    def ready(self):
        # Importing the module registers the @receiver handlers.
        try:
            from . import signals  # noqa: F401
        except Exception:
            import logging
            logging.getLogger(__name__).exception(
                'admin_security: failed to load signals module'
            )
