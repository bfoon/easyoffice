from django.apps import AppConfig


class POIConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.poi"
    label = "poi"
    verbose_name = "Persons of Interest"
