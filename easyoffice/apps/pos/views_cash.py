"""
apps/pos/views_cash.py
──────────────────────
Views for cash-drawer sessions: open the till with a float, record
pay-ins / pay-outs, and close with a physical count that yields the
over/short variance.

Wire the routes in apps/pos/urls.py (see urls_cash_patch.py).

Optional enforcement: set

    POS_REQUIRE_CASH_SESSION = True

in settings to REQUIRE an open session before any CASH sale can be
completed (the balance only tallies if every cash sale runs through a
session). Non-cash sales are unaffected. See the CompleteSaleView patch
in views_complete_patch.py.
"""
from __future__ import annotations

import json
import logging

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.utils import timezone
from django.views.generic import View, TemplateView

from .models_cash import POSCashSession
from .services import POSError
from . import services_cash
from .views import POSAccessMixin, can_void_sale, _json_body

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Status
# ─────────────────────────────────────────────────────────────────────────────

class CashSessionStatusView(POSAccessMixin, View):
    """
    GET /pos/cash/status/
    The cashier's current open session (with live tallies), or
    {ok:true, session:null} when the drawer is closed.
    """

    def get(self, request):
        session = services_cash.active_session(request.user)
        return JsonResponse({
            'ok': True,
            'session': services_cash.session_payload(session) if session else None,
        })


# ─────────────────────────────────────────────────────────────────────────────
# Open
# ─────────────────────────────────────────────────────────────────────────────

class CashSessionOpenView(POSAccessMixin, View):
    """
    POST /pos/cash/open/
    Body: {opening_float, register_name?, note?}
    """

    def post(self, request):
        data = _json_body(request)
        try:
            session = services_cash.open_session(
                request.user,
                opening_float=data.get('opening_float', '0'),
                register_name=data.get('register_name', ''),
                note=data.get('note', ''),
                currency=data.get('currency', 'GMD'),
            )
        except POSError as e:
            return JsonResponse({'ok': False, 'error': str(e)}, status=400)

        return JsonResponse({
            'ok': True,
            'session': services_cash.session_payload(session),
        }, status=201)


# ─────────────────────────────────────────────────────────────────────────────
# Pay in / pay out
# ─────────────────────────────────────────────────────────────────────────────

class CashMovementView(POSAccessMixin, View):
    """
    POST /pos/cash/movement/
    Body: {kind: 'pay_in'|'pay_out', amount, reason?}
    """

    def post(self, request):
        data = _json_body(request)
        session = services_cash.active_session(request.user)
        if session is None:
            return JsonResponse(
                {'ok': False, 'error': 'No open session.'}, status=400)
        try:
            services_cash.add_cash_movement(
                session,
                kind=(data.get('kind') or '').strip(),
                amount=data.get('amount', '0'),
                reason=data.get('reason', ''),
                actor=request.user,
            )
        except POSError as e:
            return JsonResponse({'ok': False, 'error': str(e)}, status=400)

        session.refresh_from_db()
        return JsonResponse({
            'ok': True,
            'session': services_cash.session_payload(session),
        })


# ─────────────────────────────────────────────────────────────────────────────
# Close
# ─────────────────────────────────────────────────────────────────────────────

class CashSessionCloseView(POSAccessMixin, View):
    """
    POST /pos/cash/close/
    Body: {counted_cash, note?}

    Closes the CALLER's own open session. (Supervisors can close any
    session from the detail page's form, below.)
    """

    def post(self, request):
        data = _json_body(request)
        session = services_cash.active_session(request.user)
        if session is None:
            return JsonResponse(
                {'ok': False, 'error': 'No open session to close.'}, status=400)
        try:
            session = services_cash.close_session(
                session,
                counted_cash=data.get('counted_cash', '0'),
                actor=request.user,
                note=data.get('note', ''),
            )
        except POSError as e:
            return JsonResponse({'ok': False, 'error': str(e)}, status=400)

        payload = services_cash.session_payload(session)
        from django.urls import reverse
        payload['report_url'] = reverse('pos_cash_session_detail',
                                        kwargs={'pk': session.pk})
        return JsonResponse({'ok': True, 'session': payload})


# ─────────────────────────────────────────────────────────────────────────────
# Detail / report page (X-report while open, Z-report once closed)
# ─────────────────────────────────────────────────────────────────────────────

class CashSessionDetailView(POSAccessMixin, TemplateView):
    """
    GET /pos/cash/<pk>/  — the reconciliation report for one shift.
    Owner or a supervisor may view. Supervisors get a close form while
    the session is still open.
    """
    template_name = 'pos/cash_session_detail.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        session = get_object_or_404(POSCashSession, pk=kwargs['pk'])

        is_owner = session.cashier_id == self.request.user.id
        is_supervisor = can_void_sale(self.request.user)
        if not (is_owner or is_supervisor):
            raise PermissionDenied('Not your session.')

        from .models import POSSale
        ctx['session'] = session
        ctx['data'] = services_cash.session_payload(session)
        ctx['can_close'] = session.is_open and (is_owner or is_supervisor)
        ctx['sales'] = (session.sales
                        .filter(status=POSSale.Status.COMPLETED)
                        .order_by('-completed_at'))
        return ctx


class CashSessionSupervisorCloseView(POSAccessMixin, View):
    """
    POST /pos/cash/<pk>/close/ — supervisor closes someone else's session
    from the detail page (form post, not JSON).
    """

    def post(self, request, pk):
        session = get_object_or_404(POSCashSession, pk=pk)
        is_owner = session.cashier_id == request.user.id
        if not (is_owner or can_void_sale(request.user)):
            raise PermissionDenied('You cannot close this session.')
        try:
            services_cash.close_session(
                session,
                counted_cash=request.POST.get('counted_cash', '0'),
                actor=request.user,
                note=request.POST.get('note', ''),
            )
        except POSError as e:
            messages.error(request, str(e))
            return redirect('pos_cash_session_detail', pk=pk)
        messages.success(request, f'Session {session.session_no} closed.')
        return redirect('pos_cash_session_detail', pk=pk)


# ─────────────────────────────────────────────────────────────────────────────
# Sessions list (day book of shifts)
# ─────────────────────────────────────────────────────────────────────────────

class CashSessionListView(POSAccessMixin, TemplateView):
    """
    GET /pos/cash/  — recent shifts. Own shifts by default; supervisors
    see everyone's with ?all=1.
    """
    template_name = 'pos/cash_session_list.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        qs = POSCashSession.objects.select_related('cashier', 'closed_by')

        show_all = (self.request.GET.get('all') == '1'
                    and can_void_sale(self.request.user))
        if not show_all:
            qs = qs.filter(cashier=self.request.user)

        ctx['sessions'] = qs.order_by('-opened_at')[:100]
        ctx['show_all'] = show_all
        ctx['can_see_all'] = can_void_sale(self.request.user)
        ctx['my_open'] = services_cash.active_session(self.request.user)
        return ctx
