"""apps/inventory/apps.py — app config + signal wiring."""
from django.apps import AppConfig


class InventoryConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.inventory'
    verbose_name = 'Inventory & Assets'

    def ready(self):
        # Hook signals so that:
        #   - Stock movements automatically update on-hand quantities
        #   - Sales orders being fulfilled trigger stock outflow
        #   - Purchase requests being delivered trigger stock inflow
        try:
            from . import signals  # noqa: F401
        except Exception:
            # Fail-soft so initial migrations don't blow up if a related app
            # hasn't been loaded yet.
            import logging
            logging.getLogger(__name__).exception(
                'Failed to wire inventory signals — continuing.'
            )
