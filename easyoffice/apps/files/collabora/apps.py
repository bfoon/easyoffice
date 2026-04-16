"""
apps/files/collabora/apps.py

AppConfig for the Collabora Online integration module. Registered as a
separate Django app so it owns its own migrations and is cleanly removable.
"""
from django.apps import AppConfig


class CollaboraConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.files.collabora'
    label = 'files_collabora'
    verbose_name = 'Files — Collabora Collaborative Editor'

    def ready(self):
        # Importing signals has the side-effect of connecting them.
        # We keep the import lazy to avoid AppRegistryNotReady at startup.
        from . import signals  # noqa: F401
