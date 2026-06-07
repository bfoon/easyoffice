"""
apps/it_support/views_technician_links.py
==========================================

NEW MODULE — lets IT staff create, share, and activate/deactivate the
tokenized technician portal links (apps.customer_portal.TechnicianPortalToken).

Views
-----
  * TechnicianLinkListView   GET  /it/technician-links/
        Lists technicians and the state of their portal link. IT can issue
        a link for anyone who doesn't have one yet.
  * TechnicianLinkActionView POST /it/technician-links/action/
        action = issue | activate | deactivate | regenerate

All views are IT-staff-only, reusing apps.it_support.views._is_it_staff.
"""
from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.shortcuts import redirect, render
from django.views.generic import TemplateView, View

from apps.core.models import User
from apps.it_support.views import _is_it_staff


# Which users can be issued a technician link. We treat members of the
# "Technician" or "IT" groups as eligible; superusers always eligible.
TECHNICIAN_GROUPS = ['Technician', 'IT']


def _eligible_technicians():
    return (
        User.objects
        .filter(is_active=True)
        .filter(groups__name__in=TECHNICIAN_GROUPS)
        .distinct()
        .order_by('first_name', 'last_name')
    )


def _build_link(request, token_obj):
    """Absolute URL for a technician's portal link."""
    path = f'/tech/{token_obj.token}/'
    return request.build_absolute_uri(path)


class TechnicianLinkListView(LoginRequiredMixin, TemplateView):
    template_name = 'it_support/technician_links.html'

    def get_context_data(self, **kwargs):
        from apps.customer_portal.models_technician_portal import TechnicianPortalToken

        ctx = super().get_context_data(**kwargs)
        if not _is_it_staff(self.request.user):
            ctx['denied'] = True
            return ctx

        techs = list(_eligible_technicians())
        tokens = {
            t.technician_id: t
            for t in TechnicianPortalToken.objects.select_related('technician')
        }

        rows = []
        for u in techs:
            tok = tokens.get(u.id)
            rows.append({
                'user': u,
                'token': tok,
                'has_link': tok is not None,
                'link': _build_link(self.request, tok) if tok else '',
                'is_active': bool(tok and tok.is_active),
                'email_bound': bool(tok and tok.is_email_bound),
                'last_used': tok.last_used_at if tok else None,
                'trusted_devices': tok.trusted_devices.count() if tok else 0,
            })

        # Also surface any tokens whose user fell out of the technician groups,
        # so IT can still deactivate them.
        listed_ids = {u.id for u in techs}
        for uid, tok in tokens.items():
            if uid not in listed_ids:
                rows.append({
                    'user': tok.technician,
                    'token': tok,
                    'has_link': True,
                    'link': _build_link(self.request, tok),
                    'is_active': tok.is_active,
                    'email_bound': tok.is_email_bound,
                    'last_used': tok.last_used_at,
                    'trusted_devices': tok.trusted_devices.count(),
                    'off_roster': True,
                })

        ctx['rows'] = rows
        ctx['active_count'] = sum(1 for r in rows if r['is_active'])
        ctx['total_links'] = sum(1 for r in rows if r['has_link'])
        ctx['is_it'] = True
        return ctx


class TechnicianLinkActionView(LoginRequiredMixin, View):
    http_method_names = ['post']

    def post(self, request):
        from apps.customer_portal.models_technician_portal import (
            TechnicianPortalToken, TechnicianTrustedDevice,
        )

        if not _is_it_staff(request.user):
            messages.error(request, 'IT staff only.')
            return redirect('it_dashboard')

        action = request.POST.get('action', '')
        user_id = request.POST.get('user_id')
        token_id = request.POST.get('token_id')

        # Resolve the target token (existing) or user (for issue).
        token_obj = None
        if token_id:
            token_obj = TechnicianPortalToken.objects.filter(pk=token_id).first()

        if action == 'issue':
            if not user_id:
                messages.error(request, 'Select a technician to issue a link to.')
                return redirect('technician_link_list')
            target = User.objects.filter(pk=user_id, is_active=True).first()
            if not target:
                messages.error(request, 'Technician not found.')
                return redirect('technician_link_list')
            tok, created = TechnicianPortalToken.objects.get_or_create(technician=target)
            if created:
                messages.success(request, f'Portal link created for {target.full_name}.')
            else:
                if not tok.is_active:
                    tok.is_active = True
                    tok.revoked_at = None
                    tok.save(update_fields=['is_active', 'revoked_at'])
                messages.info(request, f'{target.full_name} already had a link — it is active.')
            return redirect('technician_link_list')

        if not token_obj:
            messages.error(request, 'Link not found.')
            return redirect('technician_link_list')

        who = token_obj.technician.full_name

        if action == 'deactivate':
            token_obj.revoke()
            messages.success(request, f'{who}\u2019s link has been deactivated.')

        elif action == 'activate':
            token_obj.is_active = True
            token_obj.revoked_at = None
            token_obj.save(update_fields=['is_active', 'revoked_at'])
            messages.success(request, f'{who}\u2019s link has been reactivated.')

        elif action == 'regenerate':
            # Issue a brand-new token value (old link stops working) and clear
            # trusted devices so the technician re-verifies on the new link.
            from apps.customer_portal.models_technician_portal import _gen_token
            TechnicianTrustedDevice.objects.filter(token=token_obj).delete()
            token_obj.token = _gen_token()
            token_obj.is_active = True
            token_obj.revoked_at = None
            token_obj.save(update_fields=['token', 'is_active', 'revoked_at'])
            messages.success(
                request,
                f'{who}\u2019s link has been regenerated. Share the new link — '
                f'the old one no longer works.',
            )

        elif action == 'reset_devices':
            n = TechnicianTrustedDevice.objects.filter(token=token_obj).count()
            TechnicianTrustedDevice.objects.filter(token=token_obj).delete()
            messages.success(
                request,
                f'Cleared {n} trusted device(s) for {who}. They will verify by OTP next time.',
            )

        else:
            messages.error(request, 'Unknown action.')

        return redirect('technician_link_list')
