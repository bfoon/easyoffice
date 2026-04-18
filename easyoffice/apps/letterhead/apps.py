"""apps/letterhead/apps.py"""
from django.apps import AppConfig


class LetterheadConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name  = 'apps.letterhead'
    label = 'letterhead'
    verbose_name = 'Letterhead Builder'
