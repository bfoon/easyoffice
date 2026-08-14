"""
apps/inventory/license_views.py
───────────────────────────────
HTML views for the licence register. Thin — every state change goes
through `license_services`.

Gating follows the inventory pattern exactly: each view declares
`inv_module = 'licenses'` and the level it needs, and the mixin turns a
shortfall into the friendly no-access splash rather than a bare 403.

    view     browse the register, the dashboard and the report
    operate  create and edit licences, assign and release seats
    manage   renew, suspend, cancel, see the raw licence key, run the
             expiry scan by hand

Two deliberate exceptions:

  • `LicenseDetailView` also opens for the licence owner, the person it
    was bought for, and anyone holding a seat — you can always see the
    licence you depend on.
  • `MyLicensesView` needs no grant at all.
"""
from __future__ import annotations

import csv
from datetime import timedelta
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.db.models import Count, Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone
from django.views.generic import (
    CreateView, DetailView, ListView, TemplateView, UpdateView, View,
)

from . import license_services as services
from .license_forms import (
    LicenseForm, LicenseRenewForm, LicenseSeatForm, LicenseSeatReleaseForm,
    LicenseStatusForm, LicenseTypeForm,
)
from .license_models import (
    License, LicenseEvent, LicenseSeat, LicenseType, warn_days,
)
from .permissions import (
    LicenseAccessMixin, can_manage_licenses, can_operate_licenses,
    can_see_license_key, can_see_license_money, can_view_license,
    can_view_licenses,
)

User = get_user_model()


def license_flags(user) -> dict:
    """The permission flags every licence template expects."""
    return {
        'can_view_licenses':    can_view_licenses(user),
        'can_operate_licenses': can_operate_licenses(user),
        'can_manage_licenses':  can_manage_licenses(user),
        'can_see_key':          can_see_license_key(user),
        'can_see_money':        can_see_license_money(user),
    }


# ════════════════════════════════════════════════════════════════════════════
# Dashboard
# ════════════════════════════════════════════════════════════════════════════

class LicenseDashboardView(LicenseAccessMixin, TemplateView):
    """The renewal cockpit: what expires when, and what it all costs."""
    inv_level = 'view'
    template_name = 'inventory/license_dashboard.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        today = timezone.localdate()

        live = (License.objects
                .filter(is_active=True)
                .exclude(status__in=[License.Status.CANCELLED,
                                     License.Status.SUPERSEDED])
                .select_related('vendor', 'owner', 'holder_user', 'license_type'))
        live_list = list(live)

        expiring_30 = [l for l in live_list
                       if l.days_remaining is not None and 0 <= l.days_remaining <= 30]
        expiring_90 = [l for l in live_list
                       if l.days_remaining is not None and 0 <= l.days_remaining <= 90]
        expired = [l for l in live_list if l.is_expired]

        summary = services.license_cost_summary(live_list)

        ctx.update({
            'summary':       summary,
            'live_count':    len(live_list),
            'expiring_30':   sorted(expiring_30, key=lambda l: l.end_date)[:12],
            'expiring_30_n': len(expiring_30),
            'expiring_90_n': len(expiring_90),
            'expired':       sorted(expired, key=lambda l: l.end_date)[:8],
            'expired_n':     len(expired),
            'renewal_value_90': sum((l.renewal_estimate for l in expiring_90),
                                    Decimal('0.00')),
            'by_vendor':     services.cost_by_vendor(live_list)[:8],
            'underused':     [r for r in services.seat_utilisation(live_list)
                              if r['pct'] < 80][:8],
            'by_holder': [
                {'kind': label,
                 'count': sum(1 for l in live_list if l.holder_kind == key),
                 'annual': sum((l.annualised_cost for l in live_list
                                if l.holder_kind == key), Decimal('0.00'))}
                for key, label in License.Holder.choices
            ],
            'recent_events': (LicenseEvent.objects
                              .select_related('license', 'actor')
                              .order_by('-created_at')[:10]),
            'my_seats':      services.licenses_for_user(self.request.user)[:6],
            'warn_days':     warn_days(),
            'today':         today,
        })
        ctx.update(license_flags(self.request.user))
        return ctx


# ════════════════════════════════════════════════════════════════════════════
# Register
# ════════════════════════════════════════════════════════════════════════════

class LicenseListView(LicenseAccessMixin, ListView):
    inv_level = 'view'
    template_name = 'inventory/license_list.html'
    context_object_name = 'licenses'
    paginate_by = 30

    def get_queryset(self):
        qs = (License.objects
              .select_related('vendor', 'owner', 'holder_user', 'license_type')
              .annotate(seat_count=Count('assignments',
                                         filter=Q(assignments__released_at__isnull=True))))

        q = (self.request.GET.get('q') or '').strip()
        if q:
            qs = qs.filter(
                Q(name__icontains=q) | Q(reference__icontains=q) |
                Q(customer_name__icontains=q) | Q(account_email__icontains=q) |
                Q(vendor__name__icontains=q) | Q(license_type__name__icontains=q) |
                Q(notes__icontains=q)
            )

        status = (self.request.GET.get('status') or '').strip()
        if status:
            qs = qs.filter(status=status)
        else:
            qs = qs.exclude(status=License.Status.SUPERSEDED)

        holder = (self.request.GET.get('holder') or '').strip()
        if holder:
            qs = qs.filter(holder_kind=holder)

        vendor = (self.request.GET.get('vendor') or '').strip()
        if vendor:
            qs = qs.filter(vendor_id=vendor)

        today = timezone.localdate()
        window = (self.request.GET.get('expiry') or '').strip()
        if window == 'expired':
            qs = qs.filter(is_perpetual=False, end_date__lt=today)
        elif window.isdigit():
            qs = qs.filter(is_perpetual=False, end_date__gte=today,
                           end_date__lte=today + timedelta(days=int(window)))
        elif window == 'perpetual':
            qs = qs.filter(is_perpetual=True)

        order = (self.request.GET.get('sort') or 'expiry').strip()
        return qs.order_by({
            'expiry': 'end_date',
            'name':   'name',
            'newest': '-created_at',
            'seats':  '-seats',
        }.get(order, 'end_date'))

    def get_context_data(self, **kwargs):
        from .models import Supplier
        ctx = super().get_context_data(**kwargs)
        ctx.update({
            'q':        self.request.GET.get('q', ''),
            'status':   self.request.GET.get('status', ''),
            'holder':   self.request.GET.get('holder', ''),
            'vendor':   self.request.GET.get('vendor', ''),
            'expiry':   self.request.GET.get('expiry', ''),
            'sort':     self.request.GET.get('sort', 'expiry'),
            'status_choices': License.Status.choices,
            'holder_choices': License.Holder.choices,
            'vendors':  Supplier.objects.filter(is_active=True).order_by('name'),
            'summary':  services.license_cost_summary(list(self.get_queryset())),
        })
        ctx.update(license_flags(self.request.user))
        return ctx


class LicenseDetailView(LoginRequiredMixin, DetailView):
    """
    Open to the register, and to anyone with a personal stake in this
    licence — its owner, the staff member it was bought for, or a current
    seat holder.
    """
    model = License
    template_name = 'inventory/license_detail.html'
    context_object_name = 'license'

    def get_object(self, queryset=None):
        lic = get_object_or_404(
            License.objects.select_related(
                'vendor', 'owner', 'holder_user', 'license_type', 'department'),
            pk=self.kwargs['pk'],
        )
        if not can_view_license(self.request.user, lic):
            raise PermissionDenied("You don't have access to this licence.")
        return lic

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        lic = self.object
        services.refresh_status(lic)

        ctx.update({
            'seats': (lic.assignments
                      .select_related('user', 'asset', 'assigned_by')
                      .order_by('released_at', '-assigned_at')),
            'active_seats': (lic.assignments
                             .filter(released_at__isnull=True)
                             .select_related('user', 'asset')),
            'events': lic.events.select_related('actor')[:40],
            'renewals': lic.renewals.select_related('renewed_by')[:12],
            'seat_form': LicenseSeatForm(license_obj=lic),
            'renew_form': LicenseRenewForm(license_obj=lic),
            'release_form': LicenseSeatReleaseForm(),
            'warn_days': warn_days(),
        })
        ctx.update(license_flags(self.request.user))
        return ctx


class LicenseCreateView(LicenseAccessMixin, CreateView):
    inv_level = 'operate'
    model = License
    form_class = LicenseForm
    template_name = 'inventory/license_form.html'

    def get_form_kwargs(self):
        kw = super().get_form_kwargs()
        kw['user'] = self.request.user
        return kw

    def get_initial(self):
        return {'owner': self.request.user.pk,
                'start_date': timezone.localdate()}

    def form_valid(self, form):
        self.object = services.create_license(form.save(commit=False),
                                              actor=self.request.user)
        messages.success(self.request, f'Licence {self.object.reference} added.')
        return redirect(self.object.get_absolute_url())

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['is_create'] = True
        ctx.update(license_flags(self.request.user))
        return ctx


class LicenseUpdateView(LicenseAccessMixin, UpdateView):
    inv_level = 'operate'
    model = License
    form_class = LicenseForm
    template_name = 'inventory/license_form.html'

    def get_form_kwargs(self):
        kw = super().get_form_kwargs()
        kw['user'] = self.request.user
        return kw

    def form_valid(self, form):
        response = super().form_valid(form)
        services.log_license_update(self.object, actor=self.request.user,
                                    changed=form.changed_data)
        messages.success(self.request, 'Licence updated.')
        return response

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['is_create'] = False
        ctx.update(license_flags(self.request.user))
        return ctx


# ════════════════════════════════════════════════════════════════════════════
# Seats
# ════════════════════════════════════════════════════════════════════════════

class LicenseSeatAssignView(LicenseAccessMixin, View):
    inv_level = 'operate'

    def post(self, request, pk):
        lic = get_object_or_404(License, pk=pk)
        form = LicenseSeatForm(request.POST, license_obj=lic)
        if not form.is_valid():
            first = next(iter(form.errors.values()))[0]
            messages.error(request, first)
            return redirect(lic.get_absolute_url())

        cd = form.cleaned_data
        mode = cd['mode']
        try:
            services.assign_seat(
                lic,
                user=cd.get('user') if mode == 'user' else None,
                person_name=cd.get('person_name') or '',
                person_email=cd.get('person_email') or '',
                device_label=cd.get('device_label') or '',
                asset=cd.get('asset'),
                cost_override=cd.get('cost_override'),
                note=cd.get('note') or '',
                actor=request.user,
                # Only a manager may knowingly go over the seat count.
                allow_overallocation=(request.POST.get('force') == '1'
                                      and can_manage_licenses(request.user)),
            )
        except ValueError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, 'Seat assigned.')
        return redirect(lic.get_absolute_url())


class LicenseSeatReleaseView(LicenseAccessMixin, View):
    inv_level = 'operate'

    def post(self, request, pk):
        seat = get_object_or_404(
            LicenseSeat.objects.select_related('license'), pk=pk)
        reason = (request.POST.get('reason') or '').strip()
        services.release_seat(seat, actor=request.user, reason=reason)
        messages.success(request, f'Seat released — {seat.license.seats_free} now free.')
        return redirect(seat.license.get_absolute_url())


# ════════════════════════════════════════════════════════════════════════════
# Renewal & status
# ════════════════════════════════════════════════════════════════════════════

class LicenseRenewView(LicenseAccessMixin, View):
    inv_level = 'manage'

    def post(self, request, pk):
        lic = get_object_or_404(License, pk=pk)
        form = LicenseRenewForm(request.POST, license_obj=lic)
        if not form.is_valid():
            first = next(iter(form.errors.values()))[0]
            messages.error(request, first)
            return redirect(lic.get_absolute_url())

        cd = form.cleaned_data
        try:
            renewal = services.renew_license(
                lic, actor=request.user,
                term_months=cd.get('term_months'),
                new_end=cd.get('new_end'),
                seats=cd.get('seats'),
                unit_cost=cd.get('unit_cost'),
                unit_price=cd.get('unit_price'),
                invoice_ref=cd.get('invoice_ref') or '',
                notes=cd.get('notes') or '',
            )
        except ValueError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, f'Renewed through {renewal.new_end}.')
        return redirect(lic.get_absolute_url())


class LicenseStatusView(LicenseAccessMixin, View):
    inv_level = 'manage'

    def post(self, request, pk):
        lic = get_object_or_404(License, pk=pk)
        form = LicenseStatusForm(request.POST)
        if not form.is_valid():
            messages.error(request, 'Pick what to do with this licence.')
            return redirect(lic.get_absolute_url())

        cd = form.cleaned_data
        action, reason = cd['action'], cd.get('reason') or ''
        if action == 'suspend':
            services.suspend_license(lic, actor=request.user, reason=reason)
            messages.success(request, 'Licence suspended.')
        elif action == 'reactivate':
            services.reactivate_license(lic, actor=request.user, note=reason)
            messages.success(request, 'Licence reactivated.')
        else:
            services.cancel_license(lic, actor=request.user, reason=reason,
                                    release_seats=cd.get('release_seats', True))
            messages.success(request, 'Licence cancelled.')
        return redirect(lic.get_absolute_url())


class LicenseExpiryRunView(LicenseAccessMixin, View):
    """
    Run the expiry scan by hand. The nightly command does the same work —
    this button is for "I just fixed the mail settings, send them now".
    """
    inv_level = 'manage'

    def post(self, request):
        dry = request.POST.get('dry_run') == '1'
        result = services.run_expiry_scan(dry_run=dry, actor=request.user)
        verb = 'Would send' if dry else 'Sent'
        messages.success(
            request,
            f"{verb} {result['reminders']} reminder(s) and "
            f"{result['expired']} expiry notice(s) across "
            f"{result['checked']} licence(s)."
        )
        return redirect('inventory:license_dashboard')


# ════════════════════════════════════════════════════════════════════════════
# Catalogue of licence types
# ════════════════════════════════════════════════════════════════════════════

class LicenseTypeListView(LicenseAccessMixin, ListView):
    inv_level = 'view'
    template_name = 'inventory/license_type_list.html'
    context_object_name = 'types'
    paginate_by = 40

    def get_queryset(self):
        qs = (LicenseType.objects
              .select_related('vendor', 'category')
              .annotate(license_count=Count('licenses')))
        q = (self.request.GET.get('q') or '').strip()
        if q:
            qs = qs.filter(Q(name__icontains=q) | Q(code__icontains=q) |
                           Q(vendor__name__icontains=q))
        return qs.order_by('name')

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['q'] = self.request.GET.get('q', '')
        ctx.update(license_flags(self.request.user))
        return ctx


class LicenseTypeCreateView(LicenseAccessMixin, CreateView):
    inv_level = 'operate'
    model = LicenseType
    form_class = LicenseTypeForm
    template_name = 'inventory/license_type_form.html'

    def form_valid(self, form):
        messages.success(self.request, 'Licence type added.')
        return super().form_valid(form)

    def get_success_url(self):
        return reverse('inventory:license_type_list')


class LicenseTypeUpdateView(LicenseAccessMixin, UpdateView):
    inv_level = 'operate'
    model = LicenseType
    form_class = LicenseTypeForm
    template_name = 'inventory/license_type_form.html'

    def form_valid(self, form):
        messages.success(self.request, 'Licence type updated.')
        return super().form_valid(form)

    def get_success_url(self):
        return reverse('inventory:license_type_list')


# ════════════════════════════════════════════════════════════════════════════
# Report & personal view
# ════════════════════════════════════════════════════════════════════════════

class LicenseReportView(LicenseAccessMixin, TemplateView):
    """
    Cost and renewal report. `?format=csv` exports the same rows for
    finance; the HTML version has a print button that hides the chrome.
    """
    inv_level = 'view'
    template_name = 'inventory/license_report.html'

    def get(self, request, *args, **kwargs):
        if request.GET.get('format') == 'csv':
            return self._csv(request)
        return super().get(request, *args, **kwargs)

    def _queryset(self):
        qs = (License.objects
              .filter(is_active=True)
              .exclude(status=License.Status.SUPERSEDED)
              .select_related('vendor', 'owner', 'holder_user'))
        holder = (self.request.GET.get('holder') or '').strip()
        if holder:
            qs = qs.filter(holder_kind=holder)
        return qs.order_by('end_date')

    def _csv(self, request):
        if not can_see_license_money(request.user):
            raise PermissionDenied('You need operate access to export licence costs.')
        response = HttpResponse(content_type='text/csv')
        stamp = timezone.localdate().isoformat()
        response['Content-Disposition'] = f'attachment; filename="licences-{stamp}.csv"'
        writer = csv.writer(response)
        writer.writerow([
            'Reference', 'Name', 'Vendor', 'Held by', 'Status', 'Start', 'End',
            'Days left', 'Seats', 'Seats used', 'Currency', 'Cost per seat',
            'Total cost', 'Annualised cost', 'Selling price', 'Margin',
        ])
        for l in self._queryset():
            writer.writerow([
                l.reference, l.name, l.vendor.name if l.vendor_id else '',
                l.holder_label, l.get_status_display(), l.start_date,
                l.end_date or 'perpetual',
                '' if l.days_remaining is None else l.days_remaining,
                l.seats, l.seats_assigned, l.currency, l.cost_per_seat,
                l.total_cost, l.annualised_cost, l.total_price, l.margin,
            ])
        return response

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        rows = list(self._queryset())
        ctx.update({
            'rows':      rows,
            'summary':   services.license_cost_summary(rows),
            'by_vendor': services.cost_by_vendor(rows),
            'holder':    self.request.GET.get('holder', ''),
            'holder_choices': License.Holder.choices,
            'generated': timezone.now(),
        })
        ctx.update(license_flags(self.request.user))
        return ctx


class MyLicensesView(LoginRequiredMixin, TemplateView):
    """Every seat this person holds. No grant needed — it's their own list."""
    template_name = 'inventory/my_licenses.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        seats = list(services.licenses_for_user(self.request.user))
        ctx['seats'] = seats
        ctx['owned'] = (License.objects
                        .filter(owner=self.request.user, is_active=True)
                        .exclude(status=License.Status.CANCELLED)
                        .order_by('end_date'))
        ctx['expiring'] = [s for s in seats if s.license.is_expiring_soon]
        ctx.update(license_flags(self.request.user))
        return ctx
