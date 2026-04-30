"""apps/orders/apps.py"""
from django.apps import AppConfig


class OrdersConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.orders'

    def ready(self):
        # Wire up the SignatureRequest → order-advance bridge.
        from . import signals
        signals.connect_signature_signal()