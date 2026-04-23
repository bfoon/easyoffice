"""
Exposes `is_user_admin` in every template context so the navbar / sidebar
can conditionally show the 'User Administration' link.

Wire in settings.py TEMPLATES[0]['OPTIONS']['context_processors']:
    'apps.user_admin.context_processors.admin_flags',
"""
from apps.user_admin.views import is_user_admin


def admin_flags(request):
    return {'is_user_admin': is_user_admin(getattr(request, 'user', None))}
