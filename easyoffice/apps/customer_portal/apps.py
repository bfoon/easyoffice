"""
apps/customer_portal/apps.py
=============================

We connect the post_save signal in ready() rather than at module-import
time because the Contract model lives in a different app and may not be
loaded yet when this module is first parsed.
"""
from django.apps import AppConfig


class CustomerPortalConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.customer_portal'
    label = 'customer_portal'
    verbose_name = 'Customer Portal'

    def ready(self):
        # Import inside ready() to avoid 'apps not loaded' errors.
        from . import signals
        signals._connect()
