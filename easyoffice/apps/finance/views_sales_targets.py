"""
apps/finance/views_sales_targets.py
===================================

Sales Targets & Rewards.

Access rules:
    * Setting / editing / deleting targets and approving / paying rewards
      is restricted to the CEO and Office Administrators (plus superusers).
    * Sales agents who open the page see a read-only view of their OWN
      targets plus department-wide targets — no management controls.

Views:
    SalesTargetDashboardView   – main page: KPI tiles, targets w/ live
                                 progress, reward payout queue
    SalesTargetCreateView      – POST: create a target
    SalesTargetUpdateView      – POST: edit amounts / reward / notes
    SalesTargetDeleteView      – POST: delete (or deactivate) a target
    SalesTargetEvaluateView    – POST: re-evaluate ended targets now
    SalesRewardActionView      – POST: approve / reject / mark reward paid
"""
import logging
from datetime import date as _date
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.db.models import Q, Sum
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone
from django.views import View
from django.views.generic import TemplateView

from apps.finance.models_sales_targets import SalesTarget, SalesRewardPayout
from apps.finance.permissions import is_ceo, is_admin

log = logging.getLogger(__name__)


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _can_manage_targets(user) -> bool:
    """Only the CEO / Office Administrator (and superusers) manage targets."""
    return bool(
        user.is_authenticated
        and (user.is_superuser or is_ceo(user) or is_admin(user))
    )


def _audit(user, request, action_name, obj, notes=''):
    """Best-effort FinanceAuditLog row; never breaks the request."""
    try:
        from apps.finance.models import FinanceAuditLog
        action = getattr(FinanceAuditLog.Action, action_name.upper(),
                         action_name.lower())
        FinanceAuditLog.objects.create(
            actor=user,
            actor_name=(user.get_full_name() or user.username) if user else 'System',
            ip_address=request.META.get('REMOTE_ADDR'),
            action=action,
            model_name=type(obj).__name__,
            object_repr=str(obj)[:250],
            notes=notes,
        )
    except Exception:
        pass


def _parse_decimal(raw, field_label):
    raw = (raw or '').strip().replace(',', '')
    if raw == '':
        return None, None
    try:
        value = Decimal(raw)
        if value < 0:
            return None, f'{field_label} cannot be negative.'
        return value, None
    except InvalidOperation:
        return None, f'Invalid {field_label.lower()} — enter a number.'


def _dashboard_url(request):
    nxt = request.POST.get('next') or request.META.get('HTTP_REFERER')
    return nxt or reverse('sales_target_dashboard')


def _evaluate_ended_targets():
    """Create payout rows for every ended, met, active target. Idempotent."""
    created = 0
    today = timezone.localdate()
    qs = SalesTarget.objects.filter(is_active=True, payout__isnull=True)
    for target in qs:
        if target.period_end < today:
            try:
                if target.evaluate_payout():
                    created += 1
            except Exception:
                log.exception('Failed evaluating sales target %s', target.pk)
    return created


# ─── Dashboard ───────────────────────────────────────────────────────────────

class SalesTargetDashboardView(LoginRequiredMixin, TemplateView):
    template_name = 'finance/sales_targets.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        request = self.request
        user = request.user
        can_manage = _can_manage_targets(user)

        # Ensure payouts exist for any period that has just ended.
        _evaluate_ended_targets()

        today = timezone.localdate()
        try:
            year = int(request.GET.get('year') or today.year)
        except (TypeError, ValueError):
            year = today.year
        period_filter = (request.GET.get('period') or 'all').strip().lower()

        targets = (
            SalesTarget.objects
            .filter(year=year)
            .select_related('agent', 'set_by', 'payout')
        )
        if period_filter in ('monthly', 'quarterly'):
            targets = targets.filter(period_type=period_filter)

        # Non-managers only see their own targets + department targets.
        if not can_manage:
            targets = targets.filter(
                Q(agent=user) | Q(scope=SalesTarget.Scope.DEPARTMENT)
            )

        # Compute live progress once per target for the template.
        rows = []
        for t in targets:
            achieved = t.compute_achieved()
            rows.append({
                'target': t,
                'achieved': achieved,
                'pct': t.progress_pct(achieved),
                'met': achieved >= t.target_amount,
                'invoice_count': t.compute_invoice_count(),
                'payout': getattr(t, 'payout', None),
            })
        # Current periods first, then upcoming, then ended.
        rows.sort(key=lambda r: (
            0 if r['target'].is_period_current
            else (1 if not r['target'].is_period_over else 2),
            r['target'].period_start,
        ))

        # Payout queue (managers see all; agents see their own)
        payouts = (
            SalesRewardPayout.objects
            .filter(target__year=year)
            .select_related('target', 'target__agent',
                            'approved_by', 'paid_by')
        )
        if not can_manage:
            payouts = payouts.filter(
                Q(target__agent=user)
                | Q(target__scope=SalesTarget.Scope.DEPARTMENT)
            )

        # KPI tiles
        active_count = sum(1 for r in rows if r['target'].is_active
                           and not r['target'].is_period_over)
        met_count = sum(1 for r in rows if r['met'])
        pending_rewards = payouts.filter(
            status__in=[SalesRewardPayout.Status.PENDING,
                        SalesRewardPayout.Status.APPROVED],
        ).aggregate(s=Sum('reward_amount'))['s'] or Decimal('0.00')
        paid_rewards = payouts.filter(
            status=SalesRewardPayout.Status.PAID,
        ).aggregate(s=Sum('reward_amount'))['s'] or Decimal('0.00')

        # Agent choices for the create/edit modal — sales-related groups
        # first; fall back to all active users so nobody is unpickable.
        agents = []
        if can_manage:
            User = get_user_model()
            sales_users = (
                User.objects
                .filter(is_active=True,
                        groups__name__in=['Sales', 'Sales Rep',
                                          'Customer Service'])
                .distinct()
                .order_by('first_name', 'last_name', 'username')
            )
            others = (
                User.objects
                .filter(is_active=True)
                .exclude(pk__in=sales_users.values_list('pk', flat=True))
                .order_by('first_name', 'last_name', 'username')
            )
            agents = list(sales_users) + list(others)

        years = list(range(today.year + 1, today.year - 4, -1))

        ctx.update({
            'can_manage': can_manage,
            'rows': rows,
            'payouts': payouts,
            'year': year,
            'years': years,
            'period_filter': period_filter,
            'active_count': active_count,
            'met_count': met_count,
            'pending_rewards': pending_rewards,
            'paid_rewards': paid_rewards,
            'agents': agents,
            'today': today,
            'months': [(i, _date(2000, i, 1).strftime('%B')) for i in range(1, 13)],
            'quarters': [1, 2, 3, 4],
            'payment_method_choices': [
                ('cash',          'Cash'),
                ('bank_transfer', 'Bank transfer'),
                ('cheque',        'Cheque'),
                ('mobile_money',  'Mobile money'),
                ('other',         'Other'),
            ],
        })
        return ctx


# ─── Target CRUD ─────────────────────────────────────────────────────────────

class SalesTargetCreateView(LoginRequiredMixin, View):
    http_method_names = ['post']

    def post(self, request):
        if not _can_manage_targets(request.user):
            raise PermissionDenied

        scope = (request.POST.get('scope') or 'agent').strip()
        period_type = (request.POST.get('period_type') or 'monthly').strip()
        basis = (request.POST.get('basis') or 'invoiced').strip()
        currency = (request.POST.get('currency') or 'GMD').strip().upper()[:8]
        notes = (request.POST.get('notes') or '').strip()

        try:
            year = int(request.POST.get('year'))
        except (TypeError, ValueError):
            messages.error(request, 'Invalid year.')
            return redirect(_dashboard_url(request))

        month = quarter = None
        if period_type == SalesTarget.PeriodType.MONTHLY:
            try:
                month = int(request.POST.get('month'))
                assert 1 <= month <= 12
            except (TypeError, ValueError, AssertionError):
                messages.error(request, 'Select a valid month for a monthly target.')
                return redirect(_dashboard_url(request))
        else:
            try:
                quarter = int(request.POST.get('quarter'))
                assert 1 <= quarter <= 4
            except (TypeError, ValueError, AssertionError):
                messages.error(request, 'Select a valid quarter for a quarterly target.')
                return redirect(_dashboard_url(request))

        agent = None
        if scope == SalesTarget.Scope.AGENT:
            agent_id = request.POST.get('agent')
            if not agent_id:
                messages.error(request, 'Select a sales agent for an agent target.')
                return redirect(_dashboard_url(request))
            User = get_user_model()
            agent = User.objects.filter(pk=agent_id, is_active=True).first()
            if not agent:
                messages.error(request, 'Selected agent was not found.')
                return redirect(_dashboard_url(request))

        target_amount, err = _parse_decimal(request.POST.get('target_amount'),
                                            'Target amount')
        if err or target_amount is None or target_amount <= 0:
            messages.error(request, err or 'Enter a target amount greater than zero.')
            return redirect(_dashboard_url(request))

        reward_amount, err = _parse_decimal(request.POST.get('reward_amount'),
                                            'Reward amount')
        if err:
            messages.error(request, err)
            return redirect(_dashboard_url(request))
        reward_amount = reward_amount or Decimal('0.00')

        # No duplicate target for the same scope + period (+ agent).
        dupe = SalesTarget.objects.filter(
            scope=scope, period_type=period_type, year=year,
            month=month, quarter=quarter, currency=currency,
        )
        if scope == SalesTarget.Scope.AGENT:
            dupe = dupe.filter(agent=agent)
        if dupe.exists():
            messages.error(
                request,
                'A target for that scope and period already exists — '
                'edit it instead of creating a duplicate.',
            )
            return redirect(_dashboard_url(request))

        target = SalesTarget.objects.create(
            scope=scope,
            period_type=period_type,
            year=year, month=month, quarter=quarter,
            agent=agent,
            target_amount=target_amount,
            reward_amount=reward_amount,
            currency=currency,
            basis=basis,
            notes=notes,
            set_by=request.user,
        )
        _audit(request.user, request, 'CREATE', target,
               notes=f'Sales target created: {target}')
        messages.success(
            request,
            f'Target set for {target.scope_label} — {target.period_label} '
            f'({target.currency} {target.target_amount:,.2f}, reward '
            f'{target.currency} {target.reward_amount:,.2f}).',
        )
        return redirect(_dashboard_url(request))


class SalesTargetUpdateView(LoginRequiredMixin, View):
    http_method_names = ['post']

    def post(self, request, pk):
        if not _can_manage_targets(request.user):
            raise PermissionDenied
        target = get_object_or_404(SalesTarget, pk=pk)

        if hasattr(target, 'payout') and target.payout.status == SalesRewardPayout.Status.PAID:
            messages.error(request, 'This target already has a paid reward — it can no longer be edited.')
            return redirect(_dashboard_url(request))

        target_amount, err = _parse_decimal(request.POST.get('target_amount'),
                                            'Target amount')
        if err:
            messages.error(request, err)
            return redirect(_dashboard_url(request))
        if target_amount is not None:
            if target_amount <= 0:
                messages.error(request, 'Target amount must be greater than zero.')
                return redirect(_dashboard_url(request))
            target.target_amount = target_amount

        reward_amount, err = _parse_decimal(request.POST.get('reward_amount'),
                                            'Reward amount')
        if err:
            messages.error(request, err)
            return redirect(_dashboard_url(request))
        if reward_amount is not None:
            target.reward_amount = reward_amount

        basis = (request.POST.get('basis') or '').strip()
        if basis in dict(SalesTarget.Basis.choices):
            target.basis = basis

        if 'notes' in request.POST:
            target.notes = (request.POST.get('notes') or '').strip()

        target.is_active = request.POST.get('is_active', 'on') in ('on', 'true', '1')

        target.save()
        _audit(request.user, request, 'UPDATE', target,
               notes=f'Sales target updated: {target}')
        messages.success(request, f'Target for {target.scope_label} — {target.period_label} updated.')
        return redirect(_dashboard_url(request))


class SalesTargetDeleteView(LoginRequiredMixin, View):
    http_method_names = ['post']

    def post(self, request, pk):
        if not _can_manage_targets(request.user):
            raise PermissionDenied
        target = get_object_or_404(SalesTarget, pk=pk)

        payout = getattr(target, 'payout', None)
        if payout and payout.status == SalesRewardPayout.Status.PAID:
            messages.error(request, 'A paid reward exists for this target — it cannot be deleted.')
            return redirect(_dashboard_url(request))

        label = f'{target.scope_label} — {target.period_label}'
        _audit(request.user, request, 'DELETE', target,
               notes=f'Sales target deleted: {target}')
        target.delete()
        messages.success(request, f'Target deleted: {label}.')
        return redirect(_dashboard_url(request))


class SalesTargetEvaluateView(LoginRequiredMixin, View):
    """Manual 'Evaluate now' button — creates payouts for ended, met targets."""
    http_method_names = ['post']

    def post(self, request):
        if not _can_manage_targets(request.user):
            raise PermissionDenied
        created = _evaluate_ended_targets()
        if created:
            messages.success(request, f'{created} new reward payout(s) created for met targets.')
        else:
            messages.info(request, 'No new payouts — no ended targets have newly met their goal.')
        return redirect(_dashboard_url(request))


# ─── Reward payout actions ───────────────────────────────────────────────────

class SalesRewardActionView(LoginRequiredMixin, View):
    """
    POST fields:
        action = approve | reject | mark_paid
        method, reference  (mark_paid only, both optional)
        notes              (optional, appended)
    """
    http_method_names = ['post']

    def post(self, request, pk):
        if not _can_manage_targets(request.user):
            raise PermissionDenied
        payout = get_object_or_404(
            SalesRewardPayout.objects.select_related('target', 'target__agent'),
            pk=pk,
        )
        action = (request.POST.get('action') or '').strip()
        note = (request.POST.get('notes') or '').strip()
        now = timezone.now()

        if action == 'approve':
            if payout.status != SalesRewardPayout.Status.PENDING:
                messages.error(request, 'Only pending rewards can be approved.')
                return redirect(_dashboard_url(request))
            payout.status = SalesRewardPayout.Status.APPROVED
            payout.approved_by = request.user
            payout.approved_at = now

        elif action == 'reject':
            if payout.status == SalesRewardPayout.Status.PAID:
                messages.error(request, 'A paid reward cannot be rejected.')
                return redirect(_dashboard_url(request))
            payout.status = SalesRewardPayout.Status.REJECTED
            payout.approved_by = request.user
            payout.approved_at = now

        elif action == 'mark_paid':
            if payout.status not in (SalesRewardPayout.Status.PENDING,
                                     SalesRewardPayout.Status.APPROVED):
                messages.error(request, 'Only pending or approved rewards can be marked paid.')
                return redirect(_dashboard_url(request))
            payout.status = SalesRewardPayout.Status.PAID
            if not payout.approved_by_id:
                payout.approved_by = request.user
                payout.approved_at = now
            payout.paid_by = request.user
            payout.paid_at = now
            payout.payment_method = (request.POST.get('method') or 'cash').strip()[:30]
            payout.payment_reference = (request.POST.get('reference') or '').strip()[:120]

        else:
            messages.error(request, 'Unknown action.')
            return redirect(_dashboard_url(request))

        if note:
            stamp = now.strftime('%Y-%m-%d %H:%M')
            who = request.user.get_full_name() or request.user.username
            payout.notes = (payout.notes + '\n' if payout.notes else '') + f'[{stamp} {who}] {note}'

        payout.save()
        _audit(request.user, request, 'UPDATE', payout,
               notes=f'Reward payout {action}: {payout}')

        verb = {'approve': 'approved', 'reject': 'rejected',
                'mark_paid': 'marked as paid'}[action]
        messages.success(
            request,
            f'Reward of {payout.currency} {payout.reward_amount:,.2f} for '
            f'{payout.beneficiary_label} {verb}.',
        )
        return redirect(_dashboard_url(request))
