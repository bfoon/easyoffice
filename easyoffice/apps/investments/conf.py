"""
Investments settings. Override any key in settings.py:

    INVESTMENTS = {
        'CURRENCY': 'GMD',
        'MIN_DEPOSIT': '500',
        'MIN_WITHDRAWAL': '500',
        'INVITE_DAYS': 14,
        'ACTIVATION_HOURS': 72,
        'MANAGER_GROUPS': ['Investment Manager', 'CEO'],
        'PRIVATE_ROOT': '/app/media/_private/investments',
    }
"""
import os
from decimal import Decimal

from django.conf import settings


def _cfg():
    return getattr(settings, 'INVESTMENTS', {}) or {}


def currency():
    return _cfg().get('CURRENCY') or getattr(settings, 'OFFICE_CONFIG', {}).get('CURRENCY', 'GMD')


def min_deposit():
    return Decimal(str(_cfg().get('MIN_DEPOSIT', '100')))


def min_withdrawal():
    return Decimal(str(_cfg().get('MIN_WITHDRAWAL', '100')))


def invite_days():
    return int(_cfg().get('INVITE_DAYS', 14))


def activation_hours():
    return int(_cfg().get('ACTIVATION_HOURS', 72))


def manager_groups():
    return list(_cfg().get('MANAGER_GROUPS', ['Investment Manager', 'CEO']))


def private_root():
    root = _cfg().get('PRIVATE_ROOT')
    if root:
        return root
    return os.path.join(str(settings.MEDIA_ROOT), '_private', 'investments')


def site_url():
    return getattr(settings, 'SITE_URL', '').rstrip('/')
