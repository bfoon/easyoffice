from django.apps import AppConfig


class UserAdminConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.user_admin'
    verbose_name = 'User Administration'
