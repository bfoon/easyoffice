"""
apps/marketing/views.py
=======================

Views for the Marketing module.

Access model
------------
    Marketing group  → full working access (campaigns, contacts, analytics)
    CEO / Admin      → full access incl. analytics + delete rights
    Superuser        → everything

Includes:
  - MarketingDashboardView     – KPI dashboard (CAC, LTV, LTV:CAC, funnel)
  - MarketingDashboardDataAPI  – JSON for Chart.js (trend + breakdown charts)
  - Campaign list / create / edit / detail / delete
  - CampaignExpenseAddView / CampaignExpenseDeleteView
  - Contact list / create / edit / detail / delete
  - ContactStageView           – pipeline stage transitions (stamps CAC date)
  - ContactActivityAddView     – touch log
"""
import json
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.paginator import Paginator
from django.db.models import Count, Q, Sum
from django.http import HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.generic import TemplateView, View

from apps.core.models import User
from apps.marketing.models import (
    CampaignExpense, ContactActivity, MarketingCampaign, MarketingContact,
    average_ltv, blended_cac,
)


# ── Permission helpers ───────────────────────────────────────────────────────

def _is_marketing(user):
    return user.is_superuser or user.groups.filter(
        name__in=['Marketing']).exists()


def _is_admin(user):
    return user.is_superuser or user.groups.filter(name__in=['Admin']).exists()


def _is_ceo(user):
    if user.is_superuser:
        return True
    if user.groups.filter(name__in=['CEO']).exists():
        return True
    profile = getattr(user, 'staffprofile', None)
    title = ''
    try:
        title = (
            getattr(profile.position, 'title', '')
            if profile and getattr(profile, 'position', None) else ''
        )
    except Exception:
        title = ''
    return str(title).strip().lower() == 'ceo'


def _can_view_marketing(user):
    """Dashboard, analytics, lists — Marketing, CEO, Admin."""
    return _is_marketing(user) or _is_ceo(user) or _is_admin(user)


def _can_manage_marketing(user):
    """Create / edit campaigns, expenses and contacts."""
    return _is_marketing(user) or _is_ceo(user) or _is_admin(user)


def _can_delete_marketing(user):
    """Destructive actions are reserved for CEO / Admin / superuser."""
    return _is_ceo(user) or _is_admin(user)


class MarketingAccessMixin:
    """Blocks anyone outside Marketing / CEO / Admin."""

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return super().dispatch(request, *args, **kwargs)
        if not _can_view_marketing(request.user):
            return HttpResponseForbidden(
                'You do not have access to the Marketing module.')
        return super().dispatch(request, *args, **kwargs)


# ── Misc helpers ─────────────────────────────────────────────────────────────

def _parse_decimal(raw, default=None):
    try:
        value = Decimal(str(raw).replace(',', '').strip())
        if value < 0:
            return default
        return value.quantize(Decimal('0.01'))
    except (InvalidOperation, ValueError, TypeError, AttributeError):
        return default


def _parse_date(raw):
    try:
        return date.fromisoformat((raw or '').strip())
    except (ValueError, TypeError):
        return None


def _basis(request) -> str:
    """?basis=paid | invoiced (default invoiced) — mirrors Sales Targets."""
    basis = (request.GET.get('basis') or 'invoiced').strip().lower()
    return basis if basis in ('invoiced', 'paid') else 'invoiced'


def _period(request):
    """
    ?from / ?to date range for the dashboard.
    Defaults to the last 12 full months (incl. the current one).
    """
    today = timezone.localdate()
    default_from = (today.replace(day=1) - timedelta(days=365)).replace(day=1)
    date_from = _parse_date(request.GET.get('from')) or default_from
    date_to = _parse_date(request.GET.get('to')) or today
    if date_from > date_to:
        date_from, date_to = date_to, date_from
    return date_from, date_to


def _month_buckets(date_from: date, date_to: date):
    """List of (year, month) tuples covering the period, oldest first."""
    buckets = []
    y, m = date_from.year, date_from.month
    while (y, m) <= (date_to.year, date_to.month):
        buckets.append((y, m))
        m += 1
        if m > 12:
            m, y = 1, y + 1
    return buckets


# ═════════════════════════════════════════════════════════════════════════
# Dashboard
# ═════════════════════════════════════════════════════════════════════════

class MarketingDashboardView(LoginRequiredMixin, MarketingAccessMixin,
                             TemplateView):
    """
    Marketing KPI dashboard.

    Filters:
        ?from / ?to  = period (default: last 12 months)
        ?basis       = invoiced | paid   (LTV revenue basis)
        ?currency    = ISO code (default GMD)
    """
    template_name = 'marketing/dashboard.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        request = self.request
        date_from, date_to = _period(request)
        basis = _basis(request)
        currency = (request.GET.get('currency') or 'GMD').strip().upper()

        # ── Headline KPIs ────────────────────────────────────────────────
        cac_data = blended_cac(date_from, date_to, currency=currency)
        ltv_data = average_ltv(basis=basis, currency=currency)

        ltv_cac_ratio = None
        if cac_data['cac'] and ltv_data['avg_ltv']:
            ltv_cac_ratio = round(
                float(ltv_data['avg_ltv'] / cac_data['cac']), 2)

        # ── Funnel (period) ──────────────────────────────────────────────
        contacts_period = MarketingContact.objects.filter(
            created_at__date__gte=date_from, created_at__date__lte=date_to)
        funnel = {
            'leads': contacts_period.count(),
            'qualified': contacts_period.filter(
                stage__in=[MarketingContact.Stage.QUALIFIED,
                           MarketingContact.Stage.CUSTOMER]).count(),
            'customers': MarketingContact.objects.filter(
                stage=MarketingContact.Stage.CUSTOMER,
                converted_at__date__gte=date_from,
                converted_at__date__lte=date_to).count(),
            'lost': contacts_period.filter(
                stage=MarketingContact.Stage.LOST).count(),
        }
        funnel['conversion_pct'] = (
            round(funnel['customers'] / funnel['leads'] * 100, 1)
            if funnel['leads'] else 0.0
        )

        # ── Campaign table (with per-campaign CAC) ───────────────────────
        campaigns = (
            MarketingCampaign.objects
            .filter(currency=currency)
            .filter(
                Q(end_date__isnull=True, start_date__lte=date_to)
                | Q(end_date__gte=date_from, start_date__lte=date_to)
            )
            .prefetch_related('expenses', 'contacts')
            .order_by('-start_date')
        )
        campaign_rows = []
        for c in campaigns:
            campaign_rows.append({
                'obj': c,
                'spend': c.total_spend(),
                'leads': c.leads_count(),
                'customers': c.customers_count(),
                'conversion_pct': c.conversion_rate_pct(),
                'cac': c.cac(),
                'revenue': c.attributed_revenue(basis=basis),
                'roi_pct': c.roi_pct(basis=basis),
            })

        # ── Follow-ups due (working list for marketing) ──────────────────
        follow_ups = (
            MarketingContact.objects
            .filter(stage__in=[MarketingContact.Stage.LEAD,
                               MarketingContact.Stage.QUALIFIED],
                    next_follow_up__isnull=False,
                    next_follow_up__lte=timezone.localdate() + timedelta(days=7))
            .select_related('campaign', 'assigned_to')
            .order_by('next_follow_up')[:10]
        )

        ctx.update({
            'date_from': date_from,
            'date_to': date_to,
            'basis': basis,
            'currency': currency,
            'total_spend': cac_data['spend'],
            'customers_acquired': cac_data['customers'],
            'blended_cac': cac_data['cac'],
            'avg_ltv': ltv_data['avg_ltv'],
            'total_customers': ltv_data['customers'],
            'total_revenue': ltv_data['total_revenue'],
            'ltv_cac_ratio': ltv_cac_ratio,
            'funnel': funnel,
            'campaign_rows': campaign_rows,
            'follow_ups': follow_ups,
            'active_campaigns': MarketingCampaign.objects.filter(
                status=MarketingCampaign.Status.ACTIVE).count(),
            'can_manage': _can_manage_marketing(self.request.user),
            'is_ceo': _is_ceo(self.request.user),
            'is_admin': _is_admin(self.request.user),
        })
        return ctx


class MarketingDashboardDataAPI(LoginRequiredMixin, MarketingAccessMixin,
                                View):
    """
    JSON for Chart.js so the dashboard charts can refresh when filters
    change without a full page reload.

    Returns:
        monthly:   labels + spend[] + customers[] + cac[]
        channels:  labels + leads[] + customers[]  (breakdown by channel)
        stages:    labels + counts[]               (pipeline snapshot)
    """

    def get(self, request, *args, **kwargs):
        date_from, date_to = _period(request)
        currency = (request.GET.get('currency') or 'GMD').strip().upper()

        # ── Monthly spend vs customers vs CAC ────────────────────────────
        buckets = _month_buckets(date_from, date_to)

        spend_rows = (
            CampaignExpense.objects
            .filter(date__gte=date_from, date__lte=date_to,
                    campaign__currency=currency)
            .values_list('date__year', 'date__month')
            .annotate(s=Sum('amount'))
        )
        spend_map = {(y, m): s for y, m, s in spend_rows}

        conv_rows = (
            MarketingContact.objects
            .filter(stage=MarketingContact.Stage.CUSTOMER,
                    converted_at__date__gte=date_from,
                    converted_at__date__lte=date_to)
            .values_list('converted_at__year', 'converted_at__month')
            .annotate(c=Count('id'))
        )
        conv_map = {(y, m): c for y, m, c in conv_rows}

        labels, spend_series, cust_series, cac_series = [], [], [], []
        for (y, m) in buckets:
            labels.append(f'{y}-{m:02d}')
            spend = spend_map.get((y, m)) or Decimal('0.00')
            custs = conv_map.get((y, m)) or 0
            spend_series.append(float(spend))
            cust_series.append(custs)
            cac_series.append(
                round(float(spend / custs), 2) if custs else None)

        # ── Channel breakdown (leads + customers per campaign channel) ───
        channel_rows = (
            MarketingContact.objects
            .filter(campaign__isnull=False,
                    created_at__date__gte=date_from,
                    created_at__date__lte=date_to)
            .values('campaign__channel')
            .annotate(
                leads=Count('id'),
                customers=Count('id', filter=Q(
                    stage=MarketingContact.Stage.CUSTOMER)),
            )
            .order_by('-leads')
        )
        channel_labels = dict(MarketingCampaign.Channel.choices)
        channels = {
            'labels': [str(channel_labels.get(r['campaign__channel'],
                                              r['campaign__channel']))
                       for r in channel_rows],
            'leads': [r['leads'] for r in channel_rows],
            'customers': [r['customers'] for r in channel_rows],
        }

        # ── Pipeline snapshot (all time, current state) ──────────────────
        stage_rows = (
            MarketingContact.objects
            .values('stage').annotate(c=Count('id'))
        )
        stage_map = {r['stage']: r['c'] for r in stage_rows}
        stages = {
            'labels': ['Lead', 'Qualified', 'Customer', 'Lost'],
            'counts': [
                stage_map.get(MarketingContact.Stage.LEAD, 0),
                stage_map.get(MarketingContact.Stage.QUALIFIED, 0),
                stage_map.get(MarketingContact.Stage.CUSTOMER, 0),
                stage_map.get(MarketingContact.Stage.LOST, 0),
            ],
        }

        return JsonResponse({
            'monthly': {
                'labels': labels,
                'spend': spend_series,
                'customers': cust_series,
                'cac': cac_series,
            },
            'channels': channels,
            'stages': stages,
            'currency': currency,
        })


# ═════════════════════════════════════════════════════════════════════════
# Campaigns
# ═════════════════════════════════════════════════════════════════════════

class CampaignListView(LoginRequiredMixin, MarketingAccessMixin, TemplateView):
    """
    Filters:
        ?status  = planned | active | paused | completed | cancelled | all
        ?channel = channel key
        ?q       = free text (name, utm code, objective)
    """
    template_name = 'marketing/campaign_list.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        request = self.request
        status = (request.GET.get('status') or 'all').strip().lower()
        channel = (request.GET.get('channel') or '').strip()
        q = (request.GET.get('q') or '').strip()

        qs = (MarketingCampaign.objects
              .select_related('owner')
              .prefetch_related('expenses', 'contacts'))
        if status != 'all':
            qs = qs.filter(status=status)
        if channel:
            qs = qs.filter(channel=channel)
        if q:
            qs = qs.filter(
                Q(name__icontains=q)
                | Q(utm_code__icontains=q)
                | Q(objective__icontains=q)
            )

        rows = []
        for c in qs:
            rows.append({
                'obj': c,
                'spend': c.total_spend(),
                'leads': c.leads_count(),
                'customers': c.customers_count(),
                'cac': c.cac(),
                'budget_pct': c.budget_utilization_pct(),
            })

        paginator = Paginator(rows, 20)
        page = paginator.get_page(request.GET.get('page'))

        ctx.update({
            'page': page,
            'status': status,
            'channel': channel,
            'q': q,
            'status_choices': MarketingCampaign.Status.choices,
            'channel_choices': MarketingCampaign.Channel.choices,
            'can_manage': _can_manage_marketing(request.user),
            'can_delete': _can_delete_marketing(request.user),
        })
        return ctx


class CampaignCreateView(LoginRequiredMixin, MarketingAccessMixin, View):
    template_name = 'marketing/campaign_form.html'

    def get(self, request, *args, **kwargs):
        if not _can_manage_marketing(request.user):
            return HttpResponseForbidden('Not allowed.')
        return render(request, self.template_name, {
            'campaign': None,
            'channel_choices': MarketingCampaign.Channel.choices,
            'status_choices': MarketingCampaign.Status.choices,
            'staff': _marketing_staff_qs(),
        })

    def post(self, request, *args, **kwargs):
        if not _can_manage_marketing(request.user):
            return HttpResponseForbidden('Not allowed.')
        campaign = MarketingCampaign(created_by=request.user)
        error = _apply_campaign_post(campaign, request)
        if error:
            messages.error(request, error)
            return render(request, self.template_name, {
                'campaign': campaign,
                'channel_choices': MarketingCampaign.Channel.choices,
                'status_choices': MarketingCampaign.Status.choices,
                'staff': _marketing_staff_qs(),
            })
        campaign.save()
        messages.success(request, f'Campaign "{campaign.name}" created.')
        return redirect('marketing_campaign_detail', pk=campaign.pk)


class CampaignUpdateView(LoginRequiredMixin, MarketingAccessMixin, View):
    template_name = 'marketing/campaign_form.html'

    def get(self, request, pk, *args, **kwargs):
        if not _can_manage_marketing(request.user):
            return HttpResponseForbidden('Not allowed.')
        campaign = get_object_or_404(MarketingCampaign, pk=pk)
        return render(request, self.template_name, {
            'campaign': campaign,
            'channel_choices': MarketingCampaign.Channel.choices,
            'status_choices': MarketingCampaign.Status.choices,
            'staff': _marketing_staff_qs(),
        })

    def post(self, request, pk, *args, **kwargs):
        if not _can_manage_marketing(request.user):
            return HttpResponseForbidden('Not allowed.')
        campaign = get_object_or_404(MarketingCampaign, pk=pk)
        error = _apply_campaign_post(campaign, request)
        if error:
            messages.error(request, error)
            return render(request, self.template_name, {
                'campaign': campaign,
                'channel_choices': MarketingCampaign.Channel.choices,
                'status_choices': MarketingCampaign.Status.choices,
                'staff': _marketing_staff_qs(),
            })
        campaign.save()
        messages.success(request, f'Campaign "{campaign.name}" updated.')
        return redirect('marketing_campaign_detail', pk=campaign.pk)


def _marketing_staff_qs():
    return (User.objects.filter(is_active=True)
            .filter(Q(groups__name__in=['Marketing', 'Admin', 'CEO'])
                    | Q(is_superuser=True))
            .distinct().order_by('first_name', 'username'))


def _apply_campaign_post(campaign: MarketingCampaign, request) -> str | None:
    """Manual POST parsing. Returns an error message or None on success."""
    name = (request.POST.get('name') or '').strip()
    if not name:
        return 'Campaign name is required.'
    start = _parse_date(request.POST.get('start_date'))
    if not start:
        return 'A valid start date is required.'
    end = _parse_date(request.POST.get('end_date'))
    if end and end < start:
        return 'End date cannot be before the start date.'

    channel = (request.POST.get('channel') or '').strip()
    if channel not in MarketingCampaign.Channel.values:
        channel = MarketingCampaign.Channel.OTHER
    status = (request.POST.get('status') or '').strip()
    if status not in MarketingCampaign.Status.values:
        status = MarketingCampaign.Status.PLANNED

    budget = _parse_decimal(request.POST.get('budget'), Decimal('0.00'))

    owner = None
    owner_id = (request.POST.get('owner') or '').strip()
    if owner_id:
        owner = User.objects.filter(pk=owner_id, is_active=True).first()

    campaign.name = name
    campaign.channel = channel
    campaign.status = status
    campaign.objective = (request.POST.get('objective') or '').strip()
    campaign.start_date = start
    campaign.end_date = end
    campaign.budget = budget
    campaign.currency = (request.POST.get('currency') or 'GMD').strip().upper()[:8]
    campaign.utm_code = (request.POST.get('utm_code') or '').strip()
    campaign.target_audience = (request.POST.get('target_audience') or '').strip()
    campaign.notes = (request.POST.get('notes') or '').strip()
    campaign.owner = owner
    return None


class CampaignDetailView(LoginRequiredMixin, MarketingAccessMixin,
                         TemplateView):
    template_name = 'marketing/campaign_detail.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        basis = _basis(self.request)
        campaign = get_object_or_404(
            MarketingCampaign.objects.select_related('owner', 'created_by'),
            pk=kwargs['pk'])

        expenses = campaign.expenses.select_related('recorded_by')
        contacts = (campaign.contacts
                    .select_related('assigned_to')
                    .order_by('-created_at'))

        contact_rows = []
        for contact in contacts:
            contact_rows.append({
                'obj': contact,
                'ltv': (contact.lifetime_value(basis=basis)
                        if contact.is_customer else None),
            })

        ctx.update({
            'campaign': campaign,
            'basis': basis,
            'expenses': expenses,
            'contact_rows': contact_rows,
            'spend': campaign.total_spend(),
            'budget_pct': campaign.budget_utilization_pct(),
            'leads': campaign.leads_count(),
            'customers': campaign.customers_count(),
            'conversion_pct': campaign.conversion_rate_pct(),
            'cac': campaign.cac(),
            'revenue': campaign.attributed_revenue(basis=basis),
            'roi_pct': campaign.roi_pct(basis=basis),
            'expense_categories': CampaignExpense.Category.choices,
            'can_manage': _can_manage_marketing(self.request.user),
            'can_delete': _can_delete_marketing(self.request.user),
        })
        return ctx


class CampaignDeleteView(LoginRequiredMixin, MarketingAccessMixin, View):

    def post(self, request, pk, *args, **kwargs):
        if not _can_delete_marketing(request.user):
            return HttpResponseForbidden(
                'Only CEO / Admin can delete campaigns.')
        campaign = get_object_or_404(MarketingCampaign, pk=pk)
        name = campaign.name
        campaign.delete()
        messages.success(request, f'Campaign "{name}" deleted.')
        return redirect('marketing_campaign_list')


class CampaignExpenseAddView(LoginRequiredMixin, MarketingAccessMixin, View):

    def post(self, request, pk, *args, **kwargs):
        if not _can_manage_marketing(request.user):
            return HttpResponseForbidden('Not allowed.')
        campaign = get_object_or_404(MarketingCampaign, pk=pk)

        amount = _parse_decimal(request.POST.get('amount'))
        if amount is None or amount <= 0:
            messages.error(request, 'A valid expense amount is required.')
            return redirect('marketing_campaign_detail', pk=campaign.pk)

        category = (request.POST.get('category') or '').strip()
        if category not in CampaignExpense.Category.values:
            category = CampaignExpense.Category.OTHER

        CampaignExpense.objects.create(
            campaign=campaign,
            date=_parse_date(request.POST.get('date')) or timezone.localdate(),
            amount=amount,
            category=category,
            description=(request.POST.get('description') or '').strip(),
            receipt=request.FILES.get('receipt'),
            recorded_by=request.user,
        )
        messages.success(
            request,
            f'Expense of {campaign.currency} {amount} recorded on '
            f'"{campaign.name}".')
        return redirect('marketing_campaign_detail', pk=campaign.pk)


class CampaignExpenseDeleteView(LoginRequiredMixin, MarketingAccessMixin,
                                View):

    def post(self, request, pk, exp_pk, *args, **kwargs):
        if not _can_delete_marketing(request.user):
            return HttpResponseForbidden(
                'Only CEO / Admin can delete recorded expenses.')
        expense = get_object_or_404(
            CampaignExpense, pk=exp_pk, campaign_id=pk)
        expense.delete()
        messages.success(request, 'Expense deleted.')
        return redirect('marketing_campaign_detail', pk=pk)


# ═════════════════════════════════════════════════════════════════════════
# Contacts
# ═════════════════════════════════════════════════════════════════════════

class ContactListView(LoginRequiredMixin, MarketingAccessMixin, TemplateView):
    """
    Filters:
        ?stage    = lead | qualified | customer | lost | all
        ?campaign = campaign UUID
        ?q        = free text (name, company, email, phone)
    """
    template_name = 'marketing/contact_list.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        request = self.request
        stage = (request.GET.get('stage') or 'all').strip().lower()
        campaign_id = (request.GET.get('campaign') or '').strip()
        q = (request.GET.get('q') or '').strip()
        basis = _basis(request)

        qs = (MarketingContact.objects
              .select_related('campaign', 'assigned_to'))
        if stage != 'all':
            qs = qs.filter(stage=stage)
        if campaign_id:
            qs = qs.filter(campaign_id=campaign_id)
        if q:
            qs = qs.filter(
                Q(full_name__icontains=q)
                | Q(company__icontains=q)
                | Q(email__icontains=q)
                | Q(phone__icontains=q)
            )

        base = MarketingContact.objects.all()
        stage_tabs = [
            {'key': 'all', 'label': 'All', 'count': base.count()},
            {'key': 'lead', 'label': 'Leads',
             'count': base.filter(stage=MarketingContact.Stage.LEAD).count()},
            {'key': 'qualified', 'label': 'Qualified',
             'count': base.filter(
                 stage=MarketingContact.Stage.QUALIFIED).count()},
            {'key': 'customer', 'label': 'Customers',
             'count': base.filter(
                 stage=MarketingContact.Stage.CUSTOMER).count()},
            {'key': 'lost', 'label': 'Lost',
             'count': base.filter(stage=MarketingContact.Stage.LOST).count()},
        ]

        paginator = Paginator(qs, 25)
        page = paginator.get_page(request.GET.get('page'))

        # Only compute LTV for customers actually on this page.
        rows = []
        for contact in page.object_list:
            rows.append({
                'obj': contact,
                'ltv': (contact.lifetime_value(basis=basis)
                        if contact.is_customer else None),
            })

        ctx.update({
            'page': page,
            'rows': rows,
            'stage': stage,
            'campaign_id': campaign_id,
            'q': q,
            'basis': basis,
            'stage_tabs': stage_tabs,
            'campaigns': MarketingCampaign.objects.order_by('-start_date'),
            'can_manage': _can_manage_marketing(request.user),
        })
        return ctx


class ContactCreateView(LoginRequiredMixin, MarketingAccessMixin, View):
    template_name = 'marketing/contact_form.html'

    def get(self, request, *args, **kwargs):
        if not _can_manage_marketing(request.user):
            return HttpResponseForbidden('Not allowed.')
        return render(request, self.template_name, {
            'contact': None,
            'stage_choices': MarketingContact.Stage.choices,
            'source_choices': MarketingContact.Source.choices,
            'campaigns': MarketingCampaign.objects.order_by('-start_date'),
            'staff': _marketing_staff_qs(),
        })

    def post(self, request, *args, **kwargs):
        if not _can_manage_marketing(request.user):
            return HttpResponseForbidden('Not allowed.')
        contact = MarketingContact(created_by=request.user)
        error = _apply_contact_post(contact, request)
        if error:
            messages.error(request, error)
            return render(request, self.template_name, {
                'contact': contact,
                'stage_choices': MarketingContact.Stage.choices,
                'source_choices': MarketingContact.Source.choices,
                'campaigns': MarketingCampaign.objects.order_by('-start_date'),
                'staff': _marketing_staff_qs(),
            })
        contact.save()
        ContactActivity.objects.create(
            contact=contact, kind=ContactActivity.Kind.NOTE,
            summary='Contact created', logged_by=request.user)
        messages.success(request, f'Contact "{contact.full_name}" created.')
        return redirect('marketing_contact_detail', pk=contact.pk)


class ContactUpdateView(LoginRequiredMixin, MarketingAccessMixin, View):
    template_name = 'marketing/contact_form.html'

    def get(self, request, pk, *args, **kwargs):
        if not _can_manage_marketing(request.user):
            return HttpResponseForbidden('Not allowed.')
        contact = get_object_or_404(MarketingContact, pk=pk)
        return render(request, self.template_name, {
            'contact': contact,
            'stage_choices': MarketingContact.Stage.choices,
            'source_choices': MarketingContact.Source.choices,
            'campaigns': MarketingCampaign.objects.order_by('-start_date'),
            'staff': _marketing_staff_qs(),
        })

    def post(self, request, pk, *args, **kwargs):
        if not _can_manage_marketing(request.user):
            return HttpResponseForbidden('Not allowed.')
        contact = get_object_or_404(MarketingContact, pk=pk)
        error = _apply_contact_post(contact, request)
        if error:
            messages.error(request, error)
            return render(request, self.template_name, {
                'contact': contact,
                'stage_choices': MarketingContact.Stage.choices,
                'source_choices': MarketingContact.Source.choices,
                'campaigns': MarketingCampaign.objects.order_by('-start_date'),
                'staff': _marketing_staff_qs(),
            })
        contact.save()
        messages.success(request, f'Contact "{contact.full_name}" updated.')
        return redirect('marketing_contact_detail', pk=contact.pk)


def _apply_contact_post(contact: MarketingContact, request) -> str | None:
    full_name = (request.POST.get('full_name') or '').strip()
    if not full_name:
        return 'Contact name is required.'

    email = (request.POST.get('email') or '').strip()
    phone = (request.POST.get('phone') or '').strip()
    company = (request.POST.get('company') or '').strip()
    if not (email or phone):
        return 'At least an email or a phone number is required.'

    # Soft duplicate guard on create: same email already on file.
    if contact._state.adding and email:
        dup = MarketingContact.objects.filter(email__iexact=email).first()
        if dup:
            return (f'A contact with the email "{email}" already exists '
                    f'({dup.full_name}). Open that contact instead of '
                    f'creating a duplicate.')

    source = (request.POST.get('source') or '').strip()
    if source not in MarketingContact.Source.values:
        source = MarketingContact.Source.OTHER

    campaign = None
    campaign_id = (request.POST.get('campaign') or '').strip()
    if campaign_id:
        campaign = MarketingCampaign.objects.filter(pk=campaign_id).first()

    assigned = None
    assigned_id = (request.POST.get('assigned_to') or '').strip()
    if assigned_id:
        assigned = User.objects.filter(pk=assigned_id, is_active=True).first()

    contact.full_name = full_name
    contact.company = company
    contact.job_title = (request.POST.get('job_title') or '').strip()
    contact.email = email
    contact.phone = phone
    contact.address = (request.POST.get('address') or '').strip()
    contact.source = source
    contact.campaign = campaign
    contact.assigned_to = assigned
    contact.next_follow_up = _parse_date(request.POST.get('next_follow_up'))
    contact.interest = (request.POST.get('interest') or '').strip()
    contact.notes = (request.POST.get('notes') or '').strip()
    return None


class ContactDetailView(LoginRequiredMixin, MarketingAccessMixin,
                        TemplateView):
    template_name = 'marketing/contact_detail.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        basis = _basis(self.request)
        contact = get_object_or_404(
            MarketingContact.objects.select_related(
                'campaign', 'assigned_to', 'created_by'),
            pk=kwargs['pk'])

        matched_invoices = (
            contact.matched_invoices_qs(basis='invoiced')
            .order_by('-invoice_date')[:50]
        )

        ctx.update({
            'contact': contact,
            'basis': basis,
            'ltv_invoiced': contact.lifetime_value(basis='invoiced'),
            'ltv_paid': contact.lifetime_value(basis='paid'),
            'invoice_count': contact.invoice_count(basis='invoiced'),
            'matched_invoices': matched_invoices,
            'activities': contact.activities.select_related('logged_by')[:100],
            'activity_kinds': ContactActivity.Kind.choices,
            'stage_choices': MarketingContact.Stage.choices,
            'can_manage': _can_manage_marketing(self.request.user),
            'can_delete': _can_delete_marketing(self.request.user),
        })
        return ctx


class ContactStageView(LoginRequiredMixin, MarketingAccessMixin, View):
    """POST: move a contact through the pipeline. Stamps converted_at."""

    def post(self, request, pk, *args, **kwargs):
        if not _can_manage_marketing(request.user):
            return HttpResponseForbidden('Not allowed.')
        contact = get_object_or_404(MarketingContact, pk=pk)

        new_stage = (request.POST.get('stage') or '').strip()
        if new_stage not in MarketingContact.Stage.values:
            messages.error(request, 'Invalid stage.')
            return redirect('marketing_contact_detail', pk=contact.pk)

        old_label = contact.get_stage_display()
        contact.set_stage(
            new_stage,
            lost_reason=(request.POST.get('lost_reason') or '').strip())

        ContactActivity.objects.create(
            contact=contact, kind=ContactActivity.Kind.STAGE,
            summary=f'Stage: {old_label} → {contact.get_stage_display()}',
            details=(request.POST.get('lost_reason') or '').strip(),
            logged_by=request.user)

        if new_stage == MarketingContact.Stage.CUSTOMER:
            messages.success(
                request,
                f'{contact.full_name} marked as Customer — this now counts '
                f'toward CAC and LTV.')
        else:
            messages.success(
                request,
                f'{contact.full_name} moved to {contact.get_stage_display()}.')
        return redirect('marketing_contact_detail', pk=contact.pk)


class ContactActivityAddView(LoginRequiredMixin, MarketingAccessMixin, View):

    def post(self, request, pk, *args, **kwargs):
        if not _can_manage_marketing(request.user):
            return HttpResponseForbidden('Not allowed.')
        contact = get_object_or_404(MarketingContact, pk=pk)

        summary = (request.POST.get('summary') or '').strip()
        if not summary:
            messages.error(request, 'An activity summary is required.')
            return redirect('marketing_contact_detail', pk=contact.pk)

        kind = (request.POST.get('kind') or '').strip()
        if kind not in ContactActivity.Kind.values:
            kind = ContactActivity.Kind.NOTE

        ContactActivity.objects.create(
            contact=contact, kind=kind, summary=summary,
            details=(request.POST.get('details') or '').strip(),
            logged_by=request.user)

        # Optional inline follow-up bump.
        next_follow_up = _parse_date(request.POST.get('next_follow_up'))
        if next_follow_up:
            contact.next_follow_up = next_follow_up
            contact.save(update_fields=['next_follow_up', 'updated_at'])

        messages.success(request, 'Activity logged.')
        return redirect('marketing_contact_detail', pk=contact.pk)


class ContactDeleteView(LoginRequiredMixin, MarketingAccessMixin, View):

    def post(self, request, pk, *args, **kwargs):
        if not _can_delete_marketing(request.user):
            return HttpResponseForbidden(
                'Only CEO / Admin can delete contacts.')
        contact = get_object_or_404(MarketingContact, pk=pk)
        name = contact.full_name
        contact.delete()
        messages.success(request, f'Contact "{name}" deleted.')
        return redirect('marketing_contact_list')
