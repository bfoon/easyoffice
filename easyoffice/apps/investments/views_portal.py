"""
apps/investments/views_portal.py
────────────────────────────────
Investor portal (/invest/) and the public onboarding pages it hosts.
"""
import csv
import json
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.contrib import messages
from django.db.models import Sum
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views import View

from apps.investments import conf
from apps.investments.models import (DepositRequest, Holding, InvestmentOpportunity,
                                     InvestorApplication, PayoutMethod,
                                     WalletTransaction, WithdrawalRequest)
from apps.investments.permissions import InvestorRequiredMixin
from apps.investments.services import distribution as dist_svc
from apps.investments.services import forecast, ledger, notify, onboarding
from apps.investments.services.ledger import LedgerError

ZERO = Decimal('0.00')


def _ip(request):
    fwd = request.META.get('HTTP_X_FORWARDED_FOR', '')
    return (fwd.split(',')[0].strip() if fwd else request.META.get('REMOTE_ADDR')) or None


# ═════════════════════════════════════════════════════════════════════════════
# Public: apply & activate
# ═════════════════════════════════════════════════════════════════════════════

class ApplyView(View):
    template = 'investments/public/apply.html'

    def _invitation(self, token):
        inv = onboarding.get_invitation(token)
        if inv is None:
            raise Http404
        return inv

    def _state(self, inv):
        app = getattr(inv, 'application', None) if hasattr(inv, 'application') else None
        if inv.status == inv.Status.REVOKED:
            return 'revoked', app
        if app and app.status != InvestorApplication.Status.INFO_REQUESTED:
            return 'submitted', app
        if inv.is_expired:
            return 'expired', app
        return 'open', app

    def _ctx(self, inv, app, data=None, errors=None):
        A = InvestorApplication
        return {
            'inv': inv, 'app': app, 'd': data or self._initial(inv, app), 'errors': errors or {},
            'id_types': A.IdType.choices, 'funds': A.Funds.choices, 'risks': A.Risk.choices,
            'horizons': A.Horizon.choices, 'methods': PayoutMethod.choices,
            'currency': conf.currency(),
            'brand': getattr(settings, 'OFFICE_CONFIG', {}).get('NAME', 'EasyOffice'),
        }

    def _initial(self, inv, app):
        if app:
            d = {f.name: getattr(app, f.name) for f in app._meta.fields
                 if f.name not in ('id_document', 'proof_of_address')}
            d['date_of_birth'] = app.date_of_birth.isoformat() if app.date_of_birth else ''
            return d
        return {'full_name': inv.full_name, 'email': inv.email, 'phone': inv.phone,
                'country': 'The Gambia', 'nationality': 'Gambian'}

    def get(self, request, token):
        inv = self._invitation(token)
        state, app = self._state(inv)
        if state != 'open':
            return render(request, 'investments/public/apply_closed.html',
                          {'state': state, 'inv': inv, 'app': app})
        onboarding.mark_opened(inv)
        return render(request, self.template, self._ctx(inv, app))

    def post(self, request, token):
        inv = self._invitation(token)
        state, app = self._state(inv)
        if state != 'open':
            return render(request, 'investments/public/apply_closed.html',
                          {'state': state, 'inv': inv, 'app': app})
        cleaned, errors = onboarding.validate_application(request.POST, request.FILES, existing=app)
        if errors:
            data = dict(request.POST.items())
            return render(request, self.template, self._ctx(inv, app, data, errors), status=400)
        try:
            onboarding.submit_application(inv, cleaned, request.FILES, ip=_ip(request))
        except onboarding.OnboardingError as exc:
            data = dict(request.POST.items())
            ctx = self._ctx(inv, app, data, {'form': str(exc)})
            ctx['form_error'] = str(exc)
            return render(request, self.template, ctx, status=400)
        return redirect('invest:apply_done', token=token)


class ApplyDoneView(View):
    def get(self, request, token):
        inv = onboarding.get_invitation(token)
        if inv is None:
            raise Http404
        return render(request, 'investments/public/apply_closed.html',
                      {'state': 'submitted', 'inv': inv, 'just_submitted': True})


class ActivateView(View):
    template = 'investments/public/activate.html'

    def get(self, request, token):
        acc = onboarding.account_for_activation(token)
        return render(request, self.template, {'acc': acc, 'token': token}, status=200 if acc else 410)

    def post(self, request, token):
        acc = onboarding.account_for_activation(token)
        if acc is None:
            return render(request, self.template, {'acc': None}, status=410)
        try:
            onboarding.activate(acc, request.POST.get('password', ''), request.POST.get('confirm', ''))
        except onboarding.OnboardingError as exc:
            return render(request, self.template, {'acc': acc, 'token': token, 'error': str(exc)}, status=400)
        messages.success(request, 'Password set. Sign in with your email and new password.')
        return redirect(reverse('login') + '?next=' + reverse('invest:dashboard'))


# ═════════════════════════════════════════════════════════════════════════════
# Investor portal
# ═════════════════════════════════════════════════════════════════════════════

def _portfolio(account):
    holdings = list(account.holdings.select_related('opportunity').order_by('-capital'))
    active = [h for h in holdings if h.status == Holding.Status.ACTIVE]
    capital = sum((h.capital for h in active), ZERO)
    principal = sum((h.principal_contributed - h.redeemed_total for h in holdings), ZERO)
    profit = sum((h.profit_total for h in holdings), ZERO)
    losses = sum((h.losses_total + h.fees_total for h in holdings), ZERO)
    contributed = sum((h.principal_contributed for h in holdings), ZERO)
    return {
        'holdings': holdings, 'active': active, 'capital': capital,
        'principal': max(principal, ZERO), 'profit': profit, 'losses': losses,
        'net_worth': account.wallet_balance + capital,
        'roi': ((profit - losses) / contributed * 100).quantize(Decimal('0.01')) if contributed else Decimal('0'),
    }


class PortalBase(InvestorRequiredMixin, View):
    def ctx(self, **extra):
        acc = self.account
        return {'account': acc, 'currency': conf.currency(), **extra,
                'pending_withdrawals': acc.withdrawals.filter(status='pending').count(),
                'pending_deposits': acc.deposits.filter(status='pending').count()}


class DashboardView(PortalBase):
    def get(self, request):
        acc = self.account
        pf = _portfolio(acc)
        history = forecast.portfolio_history(acc, 365)
        fc = forecast.portfolio_forecast(acc, 12)

        month_start = timezone.localtime().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        month_profit = (acc.holdings.filter(events__kind__in=['profit_reinvest', 'profit_paid'],
                                            events__occurred_at__gte=month_start)
                        .aggregate(s=Sum('events__amount'))['s'] or ZERO)
        next_payouts = sorted(
            [{'h': h, 'at': dist_svc.next_payout_date(h.opportunity)} for h in pf['active']],
            key=lambda r: r['at'])
        expected_12m = sum((r['profit_12m'] for r in fc['holdings']), 0)

        goal = None
        if acc.goal_amount:
            goal = {'amount': acc.goal_amount, 'label': acc.goal_label or 'My goal',
                    'pct': min(100, float(pf['net_worth'] / acc.goal_amount * 100)) if acc.goal_amount else 0,
                    'eta': self._goal_eta(pf['net_worth'], acc.goal_amount, fc)}

        updates = []
        from apps.investments.models import OpportunityUpdate
        updates = (OpportunityUpdate.objects.filter(opportunity__holdings__account=acc)
                   .select_related('opportunity').distinct()[:5])

        chart = {
            'history': history,
            'forecast': {'labels': fc['labels'], 'base': fc['totals']['base'],
                         'low': fc['totals']['low'], 'high': fc['totals']['high']},
            'alloc': [{'label': h.opportunity.title, 'value': float(h.capital)} for h in pf['active'] if h.capital],
            'wallet': float(acc.wallet_balance),
        }
        return render(request, 'investments/portal/dashboard.html', self.ctx(
            pf=pf, month_profit=month_profit, next_payouts=next_payouts[:4], goal=goal,
            expected_12m=expected_12m, fc_holdings=fc['holdings'], updates=updates,
            activity=acc.wallet_transactions.select_related('holding__opportunity')[:8],
            chart=json.dumps(chart), nav='dashboard'))

    @staticmethod
    def _goal_eta(now_value, goal, fc):
        """First month the expected projection reaches the goal.
        Each holding's projected 'profit' is its total gain whether it is
        reinvested (capital growth) or paid out (wallet cash)."""
        if now_value >= goal:
            return 'reached'
        for i, pt in enumerate(fc['totals']['base']):
            if float(now_value) + pt['profit'] >= float(goal):
                return fc['labels'][i]
        return None


class OpportunityListView(PortalBase):
    def get(self, request):
        risk = request.GET.get('risk', '')
        qs = InvestmentOpportunity.objects.filter(
            status__in=[InvestmentOpportunity.Status.OPEN, InvestmentOpportunity.Status.PAUSED])
        if risk in InvestmentOpportunity.Risk.values:
            qs = qs.filter(risk_level=risk)
        mine = {h.opportunity_id: h for h in self.account.holdings.all()}
        rows = []
        for o in qs:
            rate = forecast.opportunity_rate(o)
            rows.append({'o': o, 'rate': rate, 'annual': forecast.annual_pct(rate['daily']),
                         'raised': o.raised(), 'funded': o.funded_pct(),
                         'investors': o.investor_count(), 'mine': mine.get(o.pk)})
        sort = request.GET.get('sort', 'return')
        if sort == 'funded':
            rows.sort(key=lambda r: -r['funded'])
        elif sort == 'new':
            rows.sort(key=lambda r: r['o'].created_at, reverse=True)
        else:
            rows.sort(key=lambda r: -r['annual'])
        return render(request, 'investments/portal/opportunities.html', self.ctx(
            rows=rows, risk=risk, sort=sort, nav='opportunities'))


class OpportunityDetailView(PortalBase):
    def _opp(self, slug):
        return get_object_or_404(
            InvestmentOpportunity.objects.exclude(status=InvestmentOpportunity.Status.DRAFT)
            .prefetch_related('products'), slug=slug)

    def get(self, request, slug):
        o = self._opp(slug)
        holding = self.account.holdings.filter(opportunity=o).first()
        if o.status == InvestmentOpportunity.Status.CLOSED and not holding:
            raise Http404
        rate = forecast.opportunity_rate(o)
        dists = list(o.distributions.filter(status='posted').order_by('-period_start')[:12])
        return render(request, 'investments/portal/opportunity_detail.html', self.ctx(
            o=o, holding=holding, rate=rate, annual=forecast.annual_pct(rate['daily']),
            annual_simple=forecast.annual_pct(rate['daily'], compound=False),
            raised=o.raised(), funded=o.funded_pct(), investors=o.investor_count(),
            room=o.remaining_capacity(), next_payout=dist_svc.next_payout_date(o),
            dists=dists, updates=o.updates.all()[:10],
            products=o.products.all()[:12],
            sim=json.dumps({'daily': rate['daily'], 'low': rate['low'], 'high': rate['high'],
                            'min': float(o.min_investment), 'wallet': float(self.account.wallet_balance)}),
            yield_series=json.dumps(rate.get('series', [])),
            nav='opportunities'))

    def post(self, request, slug):
        o = self._opp(slug)
        try:
            h = ledger.invest(self.account, o, request.POST.get('amount'), actor=request.user,
                              payout_mode=request.POST.get('payout_mode'))
            messages.success(request, f'Invested. You now have {conf.currency()} {h.capital:,.2f} in {o.title}.')
            return redirect('invest:holding_detail', pk=h.pk)
        except LedgerError as exc:
            messages.error(request, str(exc))
            return redirect('invest:opportunity_detail', slug=slug)


class HoldingDetailView(PortalBase):
    def _holding(self, pk):
        return get_object_or_404(Holding.objects.select_related('opportunity'), pk=pk, account=self.account)

    def get(self, request, pk):
        h = self._holding(pk)
        rate = forecast.opportunity_rate(h.opportunity)
        proj = forecast.project(float(h.capital), rate, 12, compound=(h.payout_mode == 'reinvest'))
        events = list(h.events.order_by('occurred_at'))
        hist = {'labels': [], 'capital': [], 'profit': []}
        cum = Decimal('0')
        for e in events:
            if e.kind in ('profit_reinvest', 'profit_paid'):
                cum += e.amount
            elif e.kind in ('loss', 'fee'):
                cum -= e.amount
            hist['labels'].append(timezone.localtime(e.occurred_at).strftime('%d %b %y'))
            hist['capital'].append(float(e.capital_after))
            hist['profit'].append(float(cum))
        fee_now = (h.capital * h.opportunity.early_exit_fee_pct / 100).quantize(Decimal('0.01')) if h.is_locked else ZERO
        return render(request, 'investments/portal/holding_detail.html', self.ctx(
            h=h, o=h.opportunity, rate=rate,
            annual=forecast.annual_pct(rate['daily'], h.payout_mode == 'reinvest'),
            events=list(reversed(events))[:50], next_payout=dist_svc.next_payout_date(h.opportunity),
            fee_now=fee_now, modes=Holding.PayoutMode.choices,
            chart=json.dumps({'hist': hist, 'proj': {'labels': proj['labels'], 'base': proj['base'],
                                                     'low': proj['low'], 'high': proj['high']}}),
            nav='dashboard'))

    def post(self, request, pk):
        h = self._holding(pk)
        action = request.POST.get('action')
        try:
            if action == 'payout_mode':
                ledger.set_payout_mode(h, request.POST.get('mode'))
                messages.success(request, 'Saved. Future profit will be ' +
                                 ('added to this investment.' if h.payout_mode == 'reinvest' else 'paid into your wallet.'))
            elif action == 'redeem':
                amount = h.capital if request.POST.get('all') == '1' else request.POST.get('amount')
                h, fee = ledger.redeem(h, amount, actor=request.user)
                msg = 'Capital moved to your wallet.'
                if fee:
                    msg += f' An early-exit fee of {conf.currency()} {fee:,.2f} applied.'
                messages.success(request, msg)
        except LedgerError as exc:
            messages.error(request, str(exc))
        return redirect('invest:holding_detail', pk=pk)


class WalletView(PortalBase):
    def get(self, request):
        acc = self.account
        kind = request.GET.get('kind', '')
        txns = acc.wallet_transactions.select_related('holding__opportunity')
        if kind:
            txns = txns.filter(kind=kind)
        totals = {k: acc.wallet_transactions.filter(kind=k).aggregate(s=Sum('amount'))['s'] or ZERO
                  for k in ('deposit', 'profit', 'withdrawal', 'invest')}
        return render(request, 'investments/portal/wallet.html', self.ctx(
            txns=txns[:200], kind=kind, kinds=WalletTransaction.Kind.choices,
            deposits=acc.deposits.all()[:10], withdrawals=acc.withdrawals.all()[:10],
            methods=PayoutMethod.choices, totals=totals,
            min_deposit=conf.min_deposit(), min_withdrawal=conf.min_withdrawal(),
            pay_to=_pay_to_instructions(), nav='wallet'))

    def post(self, request):
        acc = self.account
        action = request.POST.get('action')
        try:
            if action == 'deposit':
                dep = ledger.create_deposit_request(
                    acc, amount=request.POST.get('amount'), method=request.POST.get('method', ''),
                    reference=request.POST.get('reference', ''), proof=self._proof(request),
                    note=request.POST.get('note', ''))
                notify.deposit_submitted(dep)
                messages.success(request, 'Thanks — we will confirm your deposit shortly and email you.')
            elif action == 'withdraw':
                wr = ledger.request_withdrawal(
                    acc, amount=request.POST.get('amount'), method=request.POST.get('method', ''),
                    account_name=request.POST.get('account_name', ''),
                    account_number=request.POST.get('account_number', ''),
                    bank_name=request.POST.get('bank_name', ''), note=request.POST.get('note', ''),
                    actor=request.user)
                notify.withdrawal_submitted(wr)
                messages.success(request, 'Withdrawal requested. The amount is on hold until it is paid.')
            elif action == 'cancel_withdrawal':
                wr = get_object_or_404(WithdrawalRequest, pk=request.POST.get('id'), account=acc)
                ledger.cancel_withdrawal(wr, request.user)
                messages.success(request, 'Withdrawal cancelled; the money is back in your wallet.')
        except LedgerError as exc:
            messages.error(request, str(exc))
        return redirect('invest:wallet')

    @staticmethod
    def _proof(request):
        f = request.FILES.get('proof')
        if not f:
            return None
        err = onboarding._check_upload(f, 'receipt', required=False)
        if err:
            raise LedgerError(err)
        return f


def _pay_to_instructions():
    from django.conf import settings
    return (getattr(settings, 'INVESTMENTS', {}) or {}).get('DEPOSIT_INSTRUCTIONS', [])


class StatementView(PortalBase):
    """CSV statement of every wallet line, optional ?from=YYYY-MM-DD&to=..."""

    def get(self, request):
        from django.utils.dateparse import parse_date
        acc = self.account
        qs = acc.wallet_transactions.select_related('holding__opportunity').order_by('created_at')
        f, t = parse_date(request.GET.get('from', '') or ''), parse_date(request.GET.get('to', '') or '')
        if f:
            qs = qs.filter(created_at__date__gte=f)
        if t:
            qs = qs.filter(created_at__date__lte=t)
        resp = HttpResponse(content_type='text/csv')
        resp['Content-Disposition'] = f'attachment; filename="statement-{acc.account_no}-{timezone.localdate()}.csv"'
        w = csv.writer(resp)
        w.writerow(['Date', 'Type', 'Description', 'Listing', 'Reference', f'Amount ({conf.currency()})',
                    f'Balance ({conf.currency()})'])
        for x in qs:
            w.writerow([timezone.localtime(x.created_at).strftime('%Y-%m-%d %H:%M'), x.get_kind_display(),
                        x.description, x.holding.opportunity.title if x.holding else '', x.reference,
                        f'{x.amount:.2f}', f'{x.balance_after:.2f}'])
        return resp


class ProfileView(PortalBase):
    def get(self, request):
        return render(request, 'investments/portal/profile.html', self.ctx(
            methods=PayoutMethod.choices, nav='profile'))

    def post(self, request):
        acc = self.account
        action = request.POST.get('action')
        if action == 'goal':
            raw = (request.POST.get('goal_amount') or '').replace(',', '').strip()
            try:
                acc.goal_amount = ledger.money(raw) if raw else None
            except LedgerError as exc:
                messages.error(request, str(exc))
                return redirect('invest:profile')
            acc.goal_label = request.POST.get('goal_label', '').strip()[:80]
            acc.save(update_fields=['goal_amount', 'goal_label', 'updated_at'])
            messages.success(request, 'Goal saved.')
        elif action == 'payout':
            m = request.POST.get('payout_method')
            if m in PayoutMethod.values:
                acc.payout_method = m
            acc.payout_account_name = request.POST.get('payout_account_name', '').strip()[:180]
            acc.payout_account_number = request.POST.get('payout_account_number', '').strip()[:60]
            acc.payout_bank_name = request.POST.get('payout_bank_name', '').strip()[:120]
            acc.save()
            messages.success(request, 'Payout details saved.')
        elif action == 'password':
            from django.contrib.auth import update_session_auth_hash
            from django.contrib.auth.password_validation import validate_password
            from django.core.exceptions import ValidationError
            user = request.user
            new, confirm = request.POST.get('new_password', ''), request.POST.get('confirm_password', '')
            if not user.check_password(request.POST.get('current_password', '')):
                messages.error(request, 'Your current password is not correct.')
            elif new != confirm:
                messages.error(request, 'The new passwords do not match.')
            else:
                try:
                    validate_password(new, user)
                except ValidationError as exc:
                    messages.error(request, ' '.join(exc.messages))
                else:
                    user.set_password(new)
                    user.save()
                    update_session_auth_hash(request, user)
                    messages.success(request, 'Password changed.')
        return redirect('invest:profile')
