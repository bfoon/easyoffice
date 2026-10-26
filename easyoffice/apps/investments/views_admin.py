"""
apps/investments/views_admin.py
───────────────────────────────
Staff side (/investments/) — Investment Managers, CEO group, superusers.
Class-based views with manual POST handling.
"""
import json
from datetime import timedelta
from decimal import Decimal

from django.contrib import messages
from django.db.models import Count, Q, Sum
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.clickjacking import xframe_options_sameorigin

from apps.investments import conf
from apps.investments.models import (DepositRequest, Distribution, Holding,
                                     InvestmentOpportunity, InvestorAccount,
                                     InvestorApplication, InvestorInvitation,
                                     OpportunityUpdate, ProfitAdjustment,
                                     WalletTransaction, WithdrawalRequest)
from apps.investments.permissions import InvestmentManagerMixin
from apps.investments.services import distribution as dist_svc
from apps.investments.services import forecast, notify, onboarding
from apps.investments.services.ledger import LedgerError, money
from apps.investments.services import ledger

ZERO = Decimal('0.00')


def _ctx(request, **extra):
    pending = {
        'apps': InvestorApplication.objects.filter(status=InvestorApplication.Status.SUBMITTED).count(),
        'deposits': DepositRequest.objects.filter(status=DepositRequest.Status.PENDING).count(),
        'withdrawals': WithdrawalRequest.objects.filter(status=WithdrawalRequest.Status.PENDING).count(),
        'drafts': Distribution.objects.filter(status=Distribution.Status.DRAFT).count(),
    }
    return {'pending': pending, 'currency': conf.currency(), **extra}


# ─────────────────────────────────────────────────────────────────────────────
# Dashboard
# ─────────────────────────────────────────────────────────────────────────────

class AdminDashboardView(InvestmentManagerMixin, View):
    def get(self, request):
        now = timezone.now()
        active = Holding.objects.filter(status=Holding.Status.ACTIVE)
        aum = active.aggregate(s=Sum('capital'))['s'] or ZERO
        wallets = InvestorAccount.objects.aggregate(s=Sum('wallet_balance'))['s'] or ZERO
        posted30 = Distribution.objects.filter(status=Distribution.Status.POSTED,
                                               posted_at__gte=now - timedelta(days=30))
        paid30 = posted30.aggregate(s=Sum('investor_pool'))['s'] or ZERO
        profit30 = posted30.aggregate(s=Sum('net_profit'))['s'] or ZERO
        investors = InvestorAccount.objects.filter(status=InvestorAccount.Status.ACTIVE).count()

        opps = (InvestmentOpportunity.objects.exclude(status=InvestmentOpportunity.Status.DRAFT)
                .annotate(cap=Sum('holdings__capital', filter=Q(holdings__status='active')),
                          n=Count('holdings', filter=Q(holdings__status='active', holdings__capital__gt=0))))
        opp_rows = []
        for o in opps:
            rate = forecast.opportunity_rate(o)
            opp_rows.append({'o': o, 'cap': o.cap or ZERO, 'n': o.n,
                             'funded': o.funded_pct(), 'rate': rate,
                             'annual': forecast.annual_pct(rate['daily'])})
        opp_rows.sort(key=lambda r: -r['cap'])

        aum_series = forecast.aum_history(180)
        flows = {
            'deposits': DepositRequest.objects.filter(status='confirmed', reviewed_at__gte=now - timedelta(days=30)).aggregate(s=Sum('amount'))['s'] or ZERO,
            'withdrawals': WithdrawalRequest.objects.filter(status='paid', reviewed_at__gte=now - timedelta(days=30)).aggregate(s=Sum('amount'))['s'] or ZERO,
        }
        return render(request, 'investments/admin/dashboard.html', _ctx(
            request, aum=aum, wallets=wallets, paid30=paid30, profit30=profit30,
            investors=investors, opp_rows=opp_rows, flows=flows,
            chart=json.dumps({'aum': aum_series,
                              'alloc': [{'label': r['o'].title, 'value': float(r['cap'])} for r in opp_rows if r['cap']]}),
            recent=WalletTransaction.objects.select_related('account__user')[:10],
        ))


# ─────────────────────────────────────────────────────────────────────────────
# Invitations
# ─────────────────────────────────────────────────────────────────────────────

class InvitationListView(InvestmentManagerMixin, View):
    def get(self, request):
        qs = InvestorInvitation.objects.select_related('invited_by')
        status = request.GET.get('status', '')
        if status:
            qs = qs.filter(status=status)
        return render(request, 'investments/admin/invitations.html', _ctx(
            request, invitations=qs[:300], status=status, statuses=InvestorInvitation.Status.choices))

    def post(self, request):
        recipients, bad = onboarding.parse_recipients(request.POST.get('recipients', ''))
        if not recipients:
            messages.error(request, 'Add at least one valid email address.')
            return redirect('investments:invitation_list')
        created, skipped = onboarding.create_invitations(
            recipients, message=request.POST.get('message', '').strip()[:2000], actor=request.user)
        if created:
            messages.success(request, f'Sent {len(created)} invitation{"s" if len(created) != 1 else ""}.')
        for email, why in skipped:
            messages.warning(request, f'{email}: {why}.')
        if bad:
            messages.warning(request, 'Not valid emails: ' + ', '.join(bad))
        return redirect('investments:invitation_list')


class InvitationActionView(InvestmentManagerMixin, View):
    def post(self, request, pk):
        inv = get_object_or_404(InvestorInvitation, pk=pk)
        action = request.POST.get('action')
        try:
            if action == 'resend':
                onboarding.send_invitation(inv)
                messages.success(request, f'Invitation re-sent to {inv.email}.')
            elif action == 'revoke':
                inv.status = InvestorInvitation.Status.REVOKED
                inv.save(update_fields=['status', 'updated_at'])
                messages.success(request, f'Invitation for {inv.email} revoked; the link no longer works.')
        except onboarding.OnboardingError as exc:
            messages.error(request, str(exc))
        return redirect('investments:invitation_list')


# ─────────────────────────────────────────────────────────────────────────────
# Applications
# ─────────────────────────────────────────────────────────────────────────────

class ApplicationListView(InvestmentManagerMixin, View):
    def get(self, request):
        status = request.GET.get('status', 'submitted')
        qs = InvestorApplication.objects.all()
        if status != 'all':
            qs = qs.filter(status=status)
        q = request.GET.get('q', '').strip()
        if q:
            qs = qs.filter(Q(full_name__icontains=q) | Q(email__icontains=q) | Q(phone__icontains=q))
        return render(request, 'investments/admin/applications.html', _ctx(
            request, applications=qs[:300], status=status, q=q,
            statuses=InvestorApplication.Status.choices))


class ApplicationDetailView(InvestmentManagerMixin, View):
    def get(self, request, pk):
        app = get_object_or_404(InvestorApplication.objects.select_related('invitation', 'reviewed_by'), pk=pk)
        duplicates = InvestorApplication.objects.filter(
            Q(email__iexact=app.email) | Q(id_number__iexact=app.id_number) | Q(phone=app.phone)
        ).exclude(pk=app.pk)
        return render(request, 'investments/admin/application_detail.html', _ctx(
            request, app=app, duplicates=duplicates,
            account=InvestorAccount.objects.filter(application=app).first()))

    def post(self, request, pk):
        app = get_object_or_404(InvestorApplication, pk=pk)
        action = request.POST.get('action')
        note = request.POST.get('note', '').strip()
        try:
            if action == 'approve':
                acc = onboarding.approve_application(app, request.user, note)
                messages.success(request, f'Approved. Account {acc.account_no} created and the activation email sent.')
                return redirect('investments:investor_detail', pk=acc.pk)
            if action == 'reject':
                onboarding.reject_application(app, request.user, note)
                messages.success(request, 'Application rejected and the applicant informed.')
            elif action == 'info':
                onboarding.request_info(app, request.user, note)
                messages.success(request, 'Request sent. The applicant can update the same link.')
        except onboarding.OnboardingError as exc:
            messages.error(request, str(exc))
        return redirect('investments:application_detail', pk=pk)


@method_decorator(xframe_options_sameorigin, name='dispatch')
class PrivateFileView(InvestmentManagerMixin, View):
    """Streams KYC documents and deposit proofs. Never exposed under /media/.
    Same-origin framing is allowed so the review page can preview PDFs inline."""
    FIELDS = {'id_document', 'proof_of_address', 'proof'}

    def get(self, request, kind, pk, field):
        if field not in self.FIELDS:
            raise Http404
        model = {'application': InvestorApplication, 'deposit': DepositRequest}.get(kind)
        if model is None:
            raise Http404
        obj = get_object_or_404(model, pk=pk)
        f = getattr(obj, field, None)
        if not f:
            raise Http404
        try:
            handle = f.open('rb')
        except FileNotFoundError:
            raise Http404
        resp = FileResponse(handle, as_attachment=request.GET.get('dl') == '1',
                            filename=f'{kind}-{field}{f.name[f.name.rfind("."):]}')
        resp['Cache-Control'] = 'private, no-store'
        resp['X-Content-Type-Options'] = 'nosniff'
        return resp


# ─────────────────────────────────────────────────────────────────────────────
# Investors
# ─────────────────────────────────────────────────────────────────────────────

class InvestorListView(InvestmentManagerMixin, View):
    def get(self, request):
        qs = (InvestorAccount.objects.select_related('user')
              .annotate(invested=Sum('holdings__capital', filter=Q(holdings__status='active')),
                        earned=Sum('holdings__profit_total')))
        q = request.GET.get('q', '').strip()
        if q:
            qs = qs.filter(Q(user__first_name__icontains=q) | Q(user__last_name__icontains=q) |
                           Q(user__email__icontains=q) | Q(account_no__icontains=q))
        status = request.GET.get('status', '')
        if status:
            qs = qs.filter(status=status)
        return render(request, 'investments/admin/investors.html', _ctx(
            request, investors=qs[:500], q=q, status=status, statuses=InvestorAccount.Status.choices))


class InvestorDetailView(InvestmentManagerMixin, View):
    def get(self, request, pk):
        acc = get_object_or_404(InvestorAccount.objects.select_related('user', 'application'), pk=pk)
        holdings = acc.holdings.select_related('opportunity')
        return render(request, 'investments/admin/investor_detail.html', _ctx(
            request, acc=acc, holdings=holdings,
            txns=acc.wallet_transactions.select_related('holding__opportunity')[:100],
            deposits=acc.deposits.all()[:20], withdrawals=acc.withdrawals.all()[:20],
            recon=ledger.reconcile(acc),
            invested=sum((h.capital for h in holdings if h.status == 'active'), ZERO),
            earned=sum((h.profit_total for h in holdings), ZERO)))

    def post(self, request, pk):
        acc = get_object_or_404(InvestorAccount, pk=pk)
        action = request.POST.get('action')
        try:
            if action == 'adjust':
                ledger.adjust_wallet(acc, request.POST.get('amount'), request.POST.get('note', ''), request.user)
                messages.success(request, 'Wallet adjusted.')
            elif action == 'suspend':
                acc.status = InvestorAccount.Status.SUSPENDED
                acc.save(update_fields=['status', 'updated_at'])
                messages.success(request, 'Account suspended. The investor can sign in but cannot move money.')
            elif action == 'reactivate':
                acc.status = InvestorAccount.Status.ACTIVE if acc.activated_at else InvestorAccount.Status.PENDING_ACTIVATION
                acc.save(update_fields=['status', 'updated_at'])
                messages.success(request, 'Account reactivated.')
            elif action == 'resend_activation':
                onboarding.resend_activation(acc)
                messages.success(request, 'A new activation link was emailed.')
            elif action == 'portal_only':
                acc.portal_only = request.POST.get('value') == '1'
                acc.save(update_fields=['portal_only', 'updated_at'])
                messages.success(request, 'Access scope updated.')
        except (LedgerError, onboarding.OnboardingError) as exc:
            messages.error(request, str(exc))
        return redirect('investments:investor_detail', pk=pk)


# ─────────────────────────────────────────────────────────────────────────────
# Deposits & withdrawals
# ─────────────────────────────────────────────────────────────────────────────

class DepositListView(InvestmentManagerMixin, View):
    def get(self, request):
        status = request.GET.get('status', 'pending')
        qs = DepositRequest.objects.select_related('account__user', 'reviewed_by')
        if status != 'all':
            qs = qs.filter(status=status)
        return render(request, 'investments/admin/deposits.html', _ctx(
            request, deposits=qs[:300], status=status, statuses=DepositRequest.Status.choices,
            total=qs.aggregate(s=Sum('amount'))['s'] or ZERO))

    def post(self, request):
        dep = get_object_or_404(DepositRequest, pk=request.POST.get('id'))
        note = request.POST.get('note', '').strip()
        try:
            if request.POST.get('action') == 'confirm':
                dep = ledger.confirm_deposit(dep, request.user, note)
                messages.success(request, f'Confirmed {dep.amount:,.2f} for {dep.account.display_name}.')
            else:
                dep = ledger.reject_deposit(dep, request.user, note)
                messages.success(request, 'Deposit rejected.')
            dep.account.refresh_from_db()
            notify.deposit_reviewed(dep)
        except LedgerError as exc:
            messages.error(request, str(exc))
        return redirect(request.POST.get('next') or 'investments:deposit_list')


class WithdrawalListView(InvestmentManagerMixin, View):
    def get(self, request):
        status = request.GET.get('status', 'pending')
        qs = WithdrawalRequest.objects.select_related('account__user', 'reviewed_by')
        if status != 'all':
            qs = qs.filter(status=status)
        return render(request, 'investments/admin/withdrawals.html', _ctx(
            request, withdrawals=qs[:300], status=status, statuses=WithdrawalRequest.Status.choices,
            total=qs.aggregate(s=Sum('amount'))['s'] or ZERO))

    def post(self, request):
        wr = get_object_or_404(WithdrawalRequest, pk=request.POST.get('id'))
        try:
            if request.POST.get('action') == 'paid':
                wr = ledger.mark_withdrawal_paid(wr, request.user, request.POST.get('payout_reference', ''),
                                                 request.POST.get('note', ''))
                messages.success(request, 'Marked as paid.')
            else:
                wr = ledger.reject_withdrawal(wr, request.user, request.POST.get('note', ''))
                messages.success(request, 'Rejected; the money is back in the investor’s wallet.')
            notify.withdrawal_reviewed(wr)
        except LedgerError as exc:
            messages.error(request, str(exc))
        return redirect('investments:withdrawal_list')


# ─────────────────────────────────────────────────────────────────────────────
# Opportunities
# ─────────────────────────────────────────────────────────────────────────────

def _product_model():
    from apps.inventory.models import Product
    return Product


class OpportunityListView(InvestmentManagerMixin, View):
    def get(self, request):
        rows = []
        for o in InvestmentOpportunity.objects.prefetch_related('products'):
            rows.append({'o': o, 'raised': o.raised(), 'funded': o.funded_pct(),
                         'investors': o.investor_count()})
        return render(request, 'investments/admin/opportunities.html', _ctx(request, rows=rows))


class OpportunityFormView(InvestmentManagerMixin, View):
    template = 'investments/admin/opportunity_form.html'

    def _get(self, pk):
        return get_object_or_404(InvestmentOpportunity, pk=pk) if pk else None

    def _render(self, request, opp, data, errors):
        Product = _product_model()
        products = Product.objects.filter(is_active=True).order_by('name')
        taken = {}
        for o in InvestmentOpportunity.objects.exclude(status=InvestmentOpportunity.Status.CLOSED).exclude(pk=getattr(opp, 'pk', None)).prefetch_related('products'):
            for p in o.products.all():
                taken[str(p.pk)] = o.title
        selected = set(data.get('products', [])) if data else (
            {str(p.pk) for p in opp.products.all()} if opp else set())
        d = data if data is not None else self._initial(opp)
        return render(request, self.template, _ctx(
            request, opp=opp, d=d, errors=errors, products=products,
            selected=selected, taken=taken, O=InvestmentOpportunity))

    @staticmethod
    def _initial(opp):
        """Form values for GET: the listing's values, or sensible defaults."""
        o = opp or InvestmentOpportunity()
        def n(v):
            if v is None:
                return ''
            txt = f'{v:f}'
            return txt.rstrip('0').rstrip('.') if '.' in txt else txt
        flags = ('scale_by_funding', 'allow_overfunding', 'auto_post', 'include_pos', 'include_invoices')
        d = {
            'title': o.title, 'tagline': o.tagline, 'description': o.description, 'category': o.category,
            'risk_level': o.risk_level, 'investor_share_pct': n(o.investor_share_pct),
            'target_capital': n(o.target_capital) if opp else '', 'min_investment': n(o.min_investment),
            'max_per_investor': n(o.max_per_investor), 'lock_in_days': str(o.lock_in_days),
            'early_exit_fee_pct': n(o.early_exit_fee_pct),
            'distribution_frequency': o.distribution_frequency, 'loss_policy': o.loss_policy,
            'invoice_basis': o.invoice_basis,
            'sales_start': o.sales_start.isoformat() if o.sales_start else '',
        }
        for f in flags:
            d[f] = '1' if getattr(o, f) else ''
        return d

    def get(self, request, pk=None):
        return self._render(request, self._get(pk), None, {})

    def post(self, request, pk=None):
        opp = self._get(pk) or InvestmentOpportunity(created_by=request.user)
        P = request.POST
        errors, d = {}, {k: P.get(k, '').strip() for k in P.keys()}
        d['products'] = P.getlist('products')

        opp.title = d.get('title', '')[:160]
        if not opp.title:
            errors['title'] = 'Give the listing a title.'
        opp.tagline = d.get('tagline', '')[:220]
        opp.description = P.get('description', '').strip()
        opp.category = d.get('category', '')[:80]
        for key, choices in (('risk_level', InvestmentOpportunity.Risk),
                             ('distribution_frequency', InvestmentOpportunity.Frequency),
                             ('loss_policy', InvestmentOpportunity.LossPolicy),
                             ('invoice_basis', InvestmentOpportunity.InvoiceBasis)):
            if d.get(key) in choices.values:
                setattr(opp, key, d[key])

        def dec(key, label, lo=Decimal('0'), hi=None, allow_blank=False):
            raw = d.get(key, '').replace(',', '')
            if raw == '' and allow_blank:
                return None
            try:
                v = Decimal(raw).quantize(Decimal('0.01'))
            except Exception:
                errors[key] = f'Enter {label} as a number.'
                return Decimal('0')
            if v < lo or (hi is not None and v > hi):
                errors[key] = f'{label.capitalize()} must be between {lo} and {hi}.' if hi else f'{label.capitalize()} cannot be negative.'
            return v

        opp.investor_share_pct = dec('investor_share_pct', 'investor share', Decimal('0'), Decimal('100'))
        opp.target_capital = dec('target_capital', 'target capital')
        opp.min_investment = dec('min_investment', 'minimum investment')
        opp.max_per_investor = dec('max_per_investor', 'maximum per investor', allow_blank=True)
        opp.early_exit_fee_pct = dec('early_exit_fee_pct', 'early-exit fee', Decimal('0'), Decimal('50'))
        try:
            opp.lock_in_days = max(0, int(d.get('lock_in_days') or 0))
        except ValueError:
            errors['lock_in_days'] = 'Enter whole days.'
        opp.scale_by_funding = P.get('scale_by_funding') == '1'
        opp.allow_overfunding = P.get('allow_overfunding') == '1'
        opp.auto_post = P.get('auto_post') == '1'
        opp.include_pos = P.get('include_pos') == '1'
        opp.include_invoices = P.get('include_invoices') == '1'
        sd = parse_date(d.get('sales_start', ''))
        if sd:
            opp.sales_start = sd
        if not d['products']:
            errors['products'] = 'Link at least one inventory item — its sales generate the profit.'
        if request.FILES.get('cover_image'):
            opp.cover_image = request.FILES['cover_image']

        # An item may only earn for one live listing, or profit would be paid twice.
        clash = (InvestmentOpportunity.objects.exclude(status=InvestmentOpportunity.Status.CLOSED)
                 .exclude(pk=opp.pk).filter(products__pk__in=d['products']).distinct())
        if clash.exists():
            errors['products'] = 'Some items are already linked to: ' + ', '.join(c.title for c in clash)

        if errors:
            return self._render(request, opp if opp.pk else None, d, errors)
        opp.save()
        opp.products.set(_product_model().objects.filter(pk__in=d['products']))
        messages.success(request, f'“{opp.title}” saved.')
        return redirect('investments:opportunity_detail', pk=opp.pk)


class OpportunityDetailView(InvestmentManagerMixin, View):
    def get(self, request, pk):
        from apps.investments.services.profit import compute_profit
        opp = get_object_or_404(InvestmentOpportunity.objects.prefetch_related('products'), pk=pk)
        start, end = dist_svc.period_containing(opp.distribution_frequency, timezone.now())
        live = dist_svc.compute_distribution(opp, start, timezone.now())
        rate = forecast.opportunity_rate(opp)
        holders = opp.holdings.select_related('account__user').order_by('-capital')
        return render(request, 'investments/admin/opportunity_detail.html', _ctx(
            request, opp=opp, raised=opp.raised(), funded=opp.funded_pct(),
            holders=holders, live=live, period_start=start, period_end=end,
            rate=rate, annual=forecast.annual_pct(rate['daily']),
            distributions=opp.distributions.all()[:24], updates=opp.updates.all()[:20],
            adjustments=opp.adjustments.all()[:50], due=dist_svc.due_periods(opp),
            Kinds=ProfitAdjustment.Kind,
            chart=json.dumps({
                'labels': [timezone.localtime(x.period_start).strftime('%d %b') for x in reversed(list(opp.distributions.all()[:24]))],
                'profit': [float(x.net_profit) for x in reversed(list(opp.distributions.all()[:24]))],
                'pool': [float(x.investor_pool) for x in reversed(list(opp.distributions.all()[:24]))],
            })))

    def post(self, request, pk):
        opp = get_object_or_404(InvestmentOpportunity, pk=pk)
        action = request.POST.get('action')
        try:
            if action == 'status':
                new = request.POST.get('status')
                if new in InvestmentOpportunity.Status.values:
                    if new == 'open' and not opp.products.exists():
                        raise LedgerError('Link items before opening the listing.')
                    opp.status = new
                    opp.save(update_fields=['status', 'updated_at'])
                    messages.success(request, f'Listing is now {opp.get_status_display().lower()}.')
            elif action == 'update':
                title, body = request.POST.get('title', '').strip(), request.POST.get('body', '').strip()
                if not title or not body:
                    raise LedgerError('An update needs a title and a message.')
                up = OpportunityUpdate.objects.create(opportunity=opp, title=title[:160], body=body,
                                                      posted_by=request.user)
                if request.POST.get('email') == '1':
                    notify.opportunity_update_posted(up)
                messages.success(request, 'Update posted.')
            elif action == 'adjustment':
                amt = money(request.POST.get('amount'))
                if amt <= 0:
                    raise LedgerError('Enter a positive amount.')
                ProfitAdjustment.objects.create(
                    opportunity=opp, amount=amt, created_by=request.user,
                    kind=request.POST.get('kind') if request.POST.get('kind') in ProfitAdjustment.Kind.values else 'expense',
                    date=parse_date(request.POST.get('date', '')) or timezone.localdate(),
                    description=request.POST.get('description', '').strip()[:300] or 'Adjustment')
                messages.success(request, 'Adjustment recorded. It counts in the period it is dated in.')
            elif action == 'delete_adjustment':
                adj = get_object_or_404(ProfitAdjustment, pk=request.POST.get('id'), opportunity=opp)
                posted = opp.distributions.filter(status='posted', period_start__date__lte=adj.date,
                                                  period_end__date__gt=adj.date).exists()
                if posted:
                    raise LedgerError('That period has already been paid out; record a correcting entry instead.')
                adj.delete()
                messages.success(request, 'Adjustment removed.')
            elif action == 'run_due':
                n = 0
                for s, e in dist_svc.due_periods(opp):
                    _, created = dist_svc.create_distribution(opp, s, e, actor=request.user)
                    n += int(created)
                messages.success(request, f'{n} period{"s" if n != 1 else ""} calculated.' if n else 'No completed periods to calculate.')
        except LedgerError as exc:
            messages.error(request, str(exc))
        return redirect('investments:opportunity_detail', pk=pk)


# ─────────────────────────────────────────────────────────────────────────────
# Distributions
# ─────────────────────────────────────────────────────────────────────────────

class DistributionListView(InvestmentManagerMixin, View):
    def get(self, request):
        status = request.GET.get('status', '')
        qs = Distribution.objects.select_related('opportunity')
        if status:
            qs = qs.filter(status=status)
        return render(request, 'investments/admin/distributions.html', _ctx(
            request, distributions=qs[:300], status=status, statuses=Distribution.Status.choices))

    def post(self, request):
        report = dist_svc.run_due_distributions(actor=request.user)
        messages.success(request, f"Calculated {report['created']} period(s): {report['posted']} posted, "
                                  f"{report['drafts']} awaiting approval, {report['skipped']} with nothing to pay."
                                  + (f" {report['errors']} failed — see logs." if report['errors'] else ''))
        return redirect('investments:distribution_list')


class DistributionDetailView(InvestmentManagerMixin, View):
    def get(self, request, pk):
        d = get_object_or_404(Distribution.objects.select_related('opportunity', 'posted_by'), pk=pk)
        return render(request, 'investments/admin/distribution_detail.html', _ctx(
            request, d=d, allocations=d.allocations.select_related('holding__account__user')))

    def post(self, request, pk):
        d = get_object_or_404(Distribution, pk=pk)
        action = request.POST.get('action')
        try:
            if action == 'post':
                dist_svc.post_distribution(d, actor=request.user)
                messages.success(request, 'Distribution posted. Investors have been credited and emailed.')
            elif action == 'recalculate':
                if d.status != Distribution.Status.DRAFT:
                    raise ValueError('Only drafts can be recalculated.')
                opp, s, e = d.opportunity, d.period_start, d.period_end
                d.delete()
                d, _ = dist_svc.create_distribution(opp, s, e, actor=request.user, auto_post=False)
                messages.success(request, 'Recalculated with the latest sales and adjustments.')
        except ValueError as exc:
            messages.error(request, str(exc))
        return redirect('investments:distribution_detail', pk=d.pk)
