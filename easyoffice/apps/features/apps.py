from django.apps import AppConfig


class FeaturesConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.features'
    verbose_name = 'Feature Control'

    def ready(self):
        # 1. Built-in catalogue for the existing EasyOffice apps.
        from apps.features import builtin  # noqa: F401  (registers on import)

        # 2. Any app can ship its own `feature_manifest.py` that calls
        #    registry.register(...). New apps plug in with zero edits here.
        from django.utils.module_loading import autodiscover_modules
        autodiscover_modules('feature_manifest')

        # 3. Cache invalidation on flag changes.
        from apps.features import signals  # noqa: F401
