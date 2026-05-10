"""
apps/customer_portal/staff_views.py
====================================

Staff-side actions for the customer portal. Mounted under the existing
customer_portal urls.

Currently exposes:

    POST /portal/contracts/<uuid:pk>/send-link/
        name='customer_portal_send_link'

Triggered by the "Send link" button in
apps/finance/templates/finance/contract_detail.html on the
"Customer Portal Access" card. Resolves the active token for the
contract, emails the link to the token's contact, and redirects back
to the contract detail page with a flashed status.

If you'd rather keep all portal views in a single file, paste the
SendPortalLinkView class body into your existing views.py and add the
URL route there instead.
"""
from __future__ import annotations

import logging

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone
from django.views.generic import View

logger = logging.getLogger(__name__)


# ── Permission ────────────────────────────────────────────────────────────

def _can_send_portal_link(user) -> bool:
    """
    Anyone allowed to manage contracts can email the portal link.
    Mirrors apps.finance.views._can_manage_contracts but doesn't import
    it (keeps customer_portal independent of finance).
    """
    if not user or not getattr(user, 'is_authenticated', False):
        return False
    if user.is_superuser or user.is_staff:
        return True
    # Group / role match — keep loose. The contract detail page itself
    # already gates on this; we just confirm the user is a known staff
    # role rather than gating on a specific group name.
    try:
        groups = {g.lower() for g in user.groups.values_list('name', flat=True)}
    except Exception:
        groups = set()
    allow = {
        'finance', 'finance officer', 'cfo',
        'ceo', 'admin', 'administrator',
        'customer service', 'customer_service', 'cs',
        'head of customer service', 'head of cs',
        'sales', 'head of sales',
    }
    return bool(groups & allow)


# ── View ──────────────────────────────────────────────────────────────────

class SendPortalLinkView(LoginRequiredMixin, View):
    """
    POST /portal/contracts/<uuid:pk>/send-link/

    Re-resolves the active PortalAccessToken for the contract, then
    emails its URL to the token's contact via the existing
    customer_portal.notifications.send_portal_invite helper.

    Behaviour:
      * 403 if the user can't manage contracts.
      * 404 if the contract doesn't exist.
      * Soft-fail (flash + redirect) for "no token", "no contact",
        "no email" — these are operational, not programming errors.
      * Hard-fail (flash error + redirect) on SMTP exceptions.
    """
    http_method_names = ['post']

    def dispatch(self, request, *args, **kwargs):
        if not _can_send_portal_link(request.user):
            return HttpResponseForbidden(
                'You do not have permission to send portal links.'
            )
        return super().dispatch(request, *args, **kwargs)

    def post(self, request, pk):
        # Lazy imports — keep this module loadable even if other apps
        # are restructured. apps.finance is the contract source of truth.
        try:
            from apps.finance.models import Contract
        except Exception:
            logger.exception('customer_portal.send_link: finance app missing')
            messages.error(request, 'Contract module is not available.')
            return self._back(pk)

        contract = get_object_or_404(Contract, pk=pk)

        # Resolve the active portal token. The model name has varied
        # historically (PortalAccessToken / ContractPortalToken) — try
        # the canonical name first, fall back to a common alternate.
        token = self._resolve_active_token(contract)
        if token is None:
            messages.warning(
                request,
                'No active portal token exists for this contract — nothing to send. '
                'Save the contract again to retrigger token issuance.',
            )
            return self._back(pk)

        contact = getattr(token, 'contact', None)
        if contact is None:
            messages.warning(
                request,
                "This portal token isn't linked to a contact, so we don't know "
                'where to send the email. Add a contact to the token and retry.',
            )
            return self._back(pk)

        email = (getattr(contact, 'email', '') or '').strip()
        if not email:
            messages.warning(
                request,
                f"{getattr(contact, 'full_name', None) or 'The contact'} "
                "doesn't have an email address on file. Add one and retry.",
            )
            return self._back(pk)

        # Hand off to the existing notification helper. It renders
        # customer_portal/email/portal_invite.{txt,html} and uses
        # SITE_URL to build an absolute portal URL.
        try:
            from apps.customer_portal import notifications as cp_notif
        except Exception:
            logger.exception('customer_portal.send_link: notifications import failed')
            messages.error(request, 'Email subsystem is not available right now.')
            return self._back(pk)

        is_renewal = self._looks_like_renewal(token)

        try:
            cp_notif.send_portal_invite(
                token=token,
                contract=contract,
                contact=contact,
                is_renewal=is_renewal,
            )
        except Exception:
            logger.exception(
                'customer_portal.send_link: send_portal_invite raised for contract=%s contact=%s',
                contract.pk, getattr(contact, 'pk', None),
            )
            messages.error(
                request,
                f"Couldn't send the portal link to {email} — the email server "
                "rejected the message. Check the server logs and try again.",
            )
            return self._back(pk)

        # Best-effort: stamp a "last sent" marker on the token so we
        # can show 'sent X ago' on the card later. Optional — gracefully
        # ignored if the column doesn't exist.
        self._stamp_last_sent(token, request.user)

        logger.info(
            'customer_portal.send_link: portal link emailed contract=%s contact=%s email=%r by user=%s',
            contract.pk, contact.pk, email, request.user.pk,
        )
        messages.success(
            request,
            f'Portal link sent to {email}.',
        )
        return self._back(pk)

    # ── Helpers ────────────────────────────────────────────────────────

    @staticmethod
    def _resolve_active_token(contract):
        """
        Find the active token for `contract`. Tries reverse relations the
        way the templatetag does, but doesn't import the templatetag
        module (keeps this view independent of template-time setup).
        """
        # First: fast path — does the contract have a related manager
        # with the conventional name?
        for attr in ('portal_tokens', 'access_tokens', 'tokens'):
            mgr = getattr(contract, attr, None)
            if mgr is None:
                continue
            try:
                qs = mgr.all()
            except Exception:
                continue
            # Prefer ordering by valid_until desc, falling back to
            # issued_at if that's not present. The whole try/except
            # wrapper means a wrong field name won't crash anything.
            try:
                tok = qs.filter(
                    revoked_at__isnull=True,
                    valid_until__gte=timezone.now(),
                ).order_by('-valid_until').first()
            except Exception:
                try:
                    tok = qs.order_by('-issued_at').first()
                except Exception:
                    tok = None
            if tok:
                return tok

        # Fallback: import the model directly.
        try:
            from apps.customer_portal.models import PortalAccessToken
        except Exception:
            return None

        try:
            return (
                PortalAccessToken.objects
                .filter(
                    contract=contract,
                    revoked_at__isnull=True,
                    valid_until__gte=timezone.now(),
                )
                .order_by('-valid_until')
                .first()
            )
        except Exception:
            logger.warning(
                'customer_portal.send_link: PortalAccessToken lookup failed',
                exc_info=True,
            )
            return None

    @staticmethod
    def _looks_like_renewal(token) -> bool:
        """
        The notification template renders slightly different copy for a
        first invite vs a renewal. We treat a token as a "renewal" if
        there's another, older token on the same contract — i.e. this
        isn't the customer's first link.
        """
        contract_id = getattr(token, 'contract_id', None)
        if not contract_id:
            return False
        try:
            from apps.customer_portal.models import PortalAccessToken
            return (
                PortalAccessToken.objects
                .filter(contract_id=contract_id)
                .exclude(pk=token.pk)
                .exists()
            )
        except Exception:
            return False

    @staticmethod
    def _stamp_last_sent(token, user) -> None:
        """
        Best-effort write of `last_sent_at` / `last_sent_by` if those
        columns exist on the token model. Silent no-op otherwise.
        """
        update_fields = []
        if hasattr(token, 'last_sent_at'):
            token.last_sent_at = timezone.now()
            update_fields.append('last_sent_at')
        if hasattr(token, 'last_sent_by'):
            token.last_sent_by = user
            update_fields.append('last_sent_by')
        if not update_fields:
            return
        try:
            token.save(update_fields=update_fields)
        except Exception:
            logger.warning(
                'customer_portal.send_link: stamp last_sent failed', exc_info=True,
            )

    @staticmethod
    def _back(pk):
        try:
            return redirect(reverse('contract_detail', kwargs={'pk': pk}))
        except Exception:
            return redirect(f'/finance/contracts/{pk}/')
