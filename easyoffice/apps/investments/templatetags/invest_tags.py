from decimal import Decimal, InvalidOperation

from django import template

from apps.investments import conf

register = template.Library()


def _d(v):
    try:
        return Decimal(str(v))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal('0')


@register.filter
def money(value, decimals=2):
    """{{ x|money }} → 'GMD 12,500.00'"""
    d = _d(value)
    sign = '−' if d < 0 else ''
    return f'{sign}{conf.currency()} {abs(d):,.{int(decimals)}f}'


@register.filter
def num(value, decimals=2):
    return f'{_d(value):,.{int(decimals)}f}'


@register.filter
def pct(value, decimals=1):
    return f'{_d(value):,.{int(decimals)}f}%'


@register.filter
def signed_money(value):
    d = _d(value)
    sign = '+' if d > 0 else ('−' if d < 0 else '')
    return f'{sign}{conf.currency()} {abs(d):,.2f}'


@register.simple_tag
def currency():
    return conf.currency()
