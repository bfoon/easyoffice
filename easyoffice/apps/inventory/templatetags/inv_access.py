"""
apps/inventory/templatetags/inv_access.py
─────────────────────────────────────────
Template helpers for the dynamic-access system.

Usage:

    {% load inv_access %}

    {% if request.user|can_view:"products" %}
        <a href="{% url 'inventory:product_list' %}">Products</a>
    {% endif %}

    {% if request.user|can_operate:"movements" %}
        <button>Receive stock</button>
    {% endif %}

    {% if request.user|can_manage:"assets" %}
        <button>Dispose asset</button>
    {% endif %}
"""
from django import template

from ..access import (
    can_manage as _can_manage,
    can_operate as _can_operate,
    can_view as _can_view,
    effective_access as _effective_access,
    Level as _Level,
)


register = template.Library()


@register.filter(name='can_view')
def can_view(user, module):
    return _can_view(user, module)


@register.filter(name='can_operate')
def can_operate(user, module):
    return _can_operate(user, module)


@register.filter(name='can_manage')
def can_manage(user, module):
    return _can_manage(user, module)


@register.simple_tag
def inv_level(user, module):
    """Returns the user's level on the module as a string (or '')."""
    lvl = _effective_access(user, module)
    return _Level.INT_TO_NAME.get(lvl, '')


@register.simple_tag
def inv_has_any(user, *modules):
    """True if the user has at least VIEW on any of the listed modules.
    Used for collapsing whole sub-nav sections that turn out empty."""
    return any(_can_view(user, m) for m in modules)
