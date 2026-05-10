"""
apps/customer_service/templatetags/customer_service_perms.py
─────────────────────────────────────────────────────────────
Template helpers that expose the permission predicates from
apps/customer_service/permissions.py to Django templates without
requiring every view to inject them into context.

Usage in a template::

    {% load customer_service_perms %}

    {% if request.user|cs_can_create_customer %}
        <a href="{% url 'customer_service_customer_create' %}">New Contact</a>
    {% endif %}

    {% if request.user|cs_can_view_oversight %}
        <a href="{% url 'customer_service_oversight' %}">Oversight</a>
    {% endif %}

Each filter returns False for AnonymousUser / None — the same default the
predicates themselves use — so guarding `request.user` is safe even on
pages that don't require authentication.

NOTE: this file lives at:
    apps/customer_service/templatetags/__init__.py        (empty)
    apps/customer_service/templatetags/customer_service_perms.py   (this)
"""
from django import template

from ..permissions import (
    can_create_customer,
    can_use_customer_service,
    can_view_full_oversight,
    can_manage_department,
    is_head_of_customer_service,
    is_head_of_sales,
)

register = template.Library()


def _safe(predicate, user):
    """Run a predicate but never let a template crash on a NoneType etc."""
    try:
        return bool(predicate(user))
    except Exception:
        return False


@register.filter(name='cs_can_create_customer')
def cs_can_create_customer(user):
    return _safe(can_create_customer, user)


@register.filter(name='cs_can_use')
def cs_can_use(user):
    return _safe(can_use_customer_service, user)


@register.filter(name='cs_can_manage_dept')
def cs_can_manage_dept(user):
    return _safe(can_manage_department, user)


@register.filter(name='cs_can_view_oversight')
def cs_can_view_oversight(user):
    return _safe(can_view_full_oversight, user)


@register.filter(name='cs_is_head_of_cs')
def cs_is_head_of_cs(user):
    return _safe(is_head_of_customer_service, user)


@register.filter(name='cs_is_head_of_sales')
def cs_is_head_of_sales(user):
    return _safe(is_head_of_sales, user)
