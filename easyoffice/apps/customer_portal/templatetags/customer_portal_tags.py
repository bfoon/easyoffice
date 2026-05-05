"""
apps/customer_portal/templatetags/customer_portal_tags.py
==========================================================

Template tags so a template (e.g. finance's contract_detail.html) can
display portal-token info without us having to modify the finance view.

Usage in template::

    {% load customer_portal_tags %}

    {% portal_token_for contract as token %}
    {% if token %}
        {{ token.token }}      ← the secret string
        {% portal_url_for token as url %}{{ url }}
    {% endif %}

We deliberately keep the API tiny — these tags are read-only and never
mutate state. Reissuing/revoking tokens is a POST flow, not a template tag.
"""
from __future__ import annotations

import logging

from django import template
from django.urls import reverse, NoReverseMatch
from django.utils import timezone

logger = logging.getLogger(__name__)
register = template.Library()


# ─────────────────────────────────────────────────────────────────────────────
# {% portal_token_for contract %}
# ─────────────────────────────────────────────────────────────────────────────

@register.simple_tag
def portal_token_for(contract):
    """
    Return the most-recent active (not revoked, not expired) PortalAccessToken
    for the given contract, or None.

    Defensive against:
      * `contract` being None (e.g. no contract context yet)
      * the customer_portal app not being installed (ImportError)
      * any DB hiccup — we never want a sidebar widget to 500 the page
    """
    if contract is None or not getattr(contract, 'pk', None):
        return None

    try:
        from apps.customer_portal.models import PortalAccessToken
    except Exception:
        logger.debug('portal_token_for: customer_portal app not importable')
        return None

    try:
        return (
            PortalAccessToken.objects
            .filter(
                contract=contract,
                revoked_at__isnull=True,
                valid_until__gt=timezone.now(),
            )
            .select_related('contact')
            .order_by('-issued_at')
            .first()
        )
    except Exception:
        logger.exception('portal_token_for: lookup failed for contract=%s', contract.pk)
        return None


# ─────────────────────────────────────────────────────────────────────────────
# {% portal_url_for token %}
# ─────────────────────────────────────────────────────────────────────────────

@register.simple_tag(takes_context=True)
def portal_url_for(context, token):
    """
    Return an absolute URL the customer can use to enter the portal.

    Prefers `request.build_absolute_uri` so we get the right scheme/host for
    the deployment automatically. Falls back to SITE_URL + path if there's
    no request in context (e.g. when this tag is used from an offline
    rendering pipeline).
    """
    if token is None or not getattr(token, 'token', None):
        return ''

    try:
        path = reverse('customer_portal_home', kwargs={'token': token.token})
    except NoReverseMatch:
        path = f'/portal/{token.token}/'

    request = context.get('request')
    if request is not None:
        try:
            return request.build_absolute_uri(path)
        except Exception:
            pass

    # Fallback: assemble from SITE_URL.
    from django.conf import settings
    base = (getattr(settings, 'SITE_URL', '') or '').strip().rstrip('/')
    if not base:
        base = 'http://localhost:8000'
    return base + path


# ─────────────────────────────────────────────────────────────────────────────
# {% tickets_for contract limit=12 %}
# ─────────────────────────────────────────────────────────────────────────────

@register.simple_tag
def tickets_for(contract, limit: int = 12):
    """
    Return up to `limit` ServiceTicket rows for the contract, with
    `current_owner` joined in to avoid n+1 queries when the template
    iterates and shows owner names.

    Returns an empty list (not None) if anything goes wrong, so the
    template's `{% if tickets %}` check still works.
    """
    if contract is None or not getattr(contract, 'pk', None):
        return []

    try:
        from apps.customer_service.models import ServiceTicket
    except Exception:
        logger.debug('tickets_for: customer_service app not importable')
        return []

    try:
        qs = (
            ServiceTicket.objects
            .filter(contract=contract)
            .select_related('current_owner', 'customer')
            .order_by('-created_at')
        )
        if limit and limit > 0:
            qs = qs[: int(limit)]
        return list(qs)
    except Exception:
        logger.exception('tickets_for: lookup failed for contract=%s', contract.pk)
        return []


# ─────────────────────────────────────────────────────────────────────────────
# {% tickets_count_for contract %}  →  total count, separate from the page slice
# ─────────────────────────────────────────────────────────────────────────────

@register.simple_tag
def tickets_count_for(contract) -> int:
    if contract is None or not getattr(contract, 'pk', None):
        return 0
    try:
        from apps.customer_service.models import ServiceTicket
        return ServiceTicket.objects.filter(contract=contract).count()
    except Exception:
        logger.exception('tickets_count_for: failed for contract=%s', contract.pk)
        return 0


@register.simple_tag
def portal_token_status(token, expiring_days: int = 14):
    """
    Lightweight status string for badge rendering.

      * "expired"   — past valid_until OR revoked
      * "expiring"  — expires within `expiring_days` days
      * "active"    — otherwise

    Returns 'none' if no token.
    """
    if token is None:
        return 'none'

    if getattr(token, 'revoked_at', None):
        return 'expired'

    valid_until = getattr(token, 'valid_until', None)
    if valid_until is None:
        return 'active'

    now = timezone.now()
    if valid_until <= now:
        return 'expired'

    if (valid_until - now).days <= expiring_days:
        return 'expiring'

    return 'active'
