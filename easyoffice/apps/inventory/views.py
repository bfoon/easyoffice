"""
apps/inventory/views.py
───────────────────────
Internal HTML views for the Inventory module. Mirrors the structure
of apps/orders/views.py — class-based views, thin layer over services.py.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.db.models import Count, F, Q, Sum
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse_lazy
from django.utils import timezone
from django.views.generic import (
    CreateView, DetailView, ListView, TemplateView, UpdateView, View,
)

from . import services
from .forms import (
    AssetAssignForm, AssetForm, AssetMaintenanceForm, AssetReturnForm,
    CategoryForm, IssueStockForm, LocationForm, ProductForm,
    ReceiveStockForm, StockRequestForm, StockTakeForm, SupplierForm,
    TransferStockForm,
)
from .models import (
    Asset, AssetAssignment, AssetEvent, AssetMaintenance, Category, Location,
    Product, StockBatch, StockItem, StockMovement, StockRequest,
    StockRequestLine, StockTake, StockTakeLine, Supplier,
)
from .permissions import (
    InventoryAccessMixin, InventoryManagerMixin,
    can_approve_inventory, can_dispose_assets, can_manage_stock,
    can_view_asset, can_view_stock_request,
)

User = get_user_model()


# ════════════════════════════════════════════════════════════════════════════
# Shared context mixin
# ════════════════════════════════════════════════════════════════════════════

class _InventoryContextMixin:
    """
    Adds permission flags to every inventory view's template context so the
    new shell (sub-nav, quick-add menu) and templates can show or hide
    actions consistently.
    """
    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        u = self.request.user
        ctx.setdefault('can_manage',  can_manage_stock(u))
        ctx.setdefault('can_approve', can_approve_inventory(u))
        ctx.setdefault('can_dispose', can_dispose_assets(u))
        return ctx


# ════════════════════════════════════════════════════════════════════════════
# Dashboard
# ════════════════════════════════════════════════════════════════════════════

class InventoryDashboardView(_InventoryContextMixin, InventoryAccessMixin, TemplateView):
    template_name = 'inventory/dashboard.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)

        # Top-line counts
        ctx['stats'] = {
            'product_count':  Product.objects.filter(is_active=True).count(),
            'asset_count':    Asset.objects.filter(is_active=True).count(),
            'location_count': Location.objects.filter(is_active=True).count(),
            'supplier_count': Supplier.objects.filter(is_active=True).count(),
        }

        # Low-stock products (computed in Python because the rule depends
        # on aggregating stock_items per product)
        low = []
        for p in (Product.objects
                  .filter(is_active=True, kind=Product.Kind.STOCKED)
                  .prefetch_related('stock_items')[:200]):
            if p.is_low_stock:
                low.append(p)
        ctx['low_stock'] = low[:10]
        ctx['low_stock_total'] = len(low)

        # Asset breakdown by status
        ctx['asset_status_breakdown'] = list(
            Asset.objects.filter(is_active=True)
            .values('status')
            .annotate(count=Count('id'))
            .order_by('status')
        )

        # Recent movements (everyone with inventory access can see this)
        ctx['recent_movements'] = (
            StockMovement.objects
            .select_related('product', 'location', 'actor')
            .order_by('-occurred_at')[:12]
        )

        # Open stock requests
        ctx['open_requests'] = (
            StockRequest.objects
            .filter(status__in=[
                StockRequest.Status.PENDING,
                StockRequest.Status.APPROVED,
                StockRequest.Status.PARTIAL,
            ])
            .select_related('requested_by', 'department')
            .order_by('-created_at')[:8]
        )

        # Assets currently assigned to me
        if self.request.user.is_authenticated:
            ctx['my_assets'] = (
                Asset.objects
                .filter(assigned_to=self.request.user, is_active=True)
                .order_by('name')[:10]
            )

        ctx['can_manage'] = can_manage_stock(self.request.user)
        return ctx


# ════════════════════════════════════════════════════════════════════════════
# Products
# ════════════════════════════════════════════════════════════════════════════

class ProductListView(_InventoryContextMixin, InventoryAccessMixin, ListView):
    template_name = 'inventory/product_list.html'
    context_object_name = 'products'
    paginate_by = 30

    def get_queryset(self):
        qs = (Product.objects
              .filter(is_active=True)
              .select_related('category', 'preferred_supplier')
              .prefetch_related('stock_items'))
        q = self.request.GET.get('q', '').strip()
        if q:
            qs = qs.filter(
                Q(sku__icontains=q) | Q(name__icontains=q) |
                Q(barcode__icontains=q) | Q(description__icontains=q)
            )
        kind = self.request.GET.get('kind', '').strip()
        if kind:
            qs = qs.filter(kind=kind)
        cat = self.request.GET.get('category', '').strip()
        if cat:
            qs = qs.filter(category_id=cat)
        status = self.request.GET.get('stock', '').strip()
        # Stock-status filter is computed; do a coarse SQL filter then refine.
        if status == 'out':
            qs = qs.filter(kind=Product.Kind.STOCKED)
        elif status == 'low':
            qs = qs.filter(kind=Product.Kind.STOCKED)
        return qs.order_by('name')

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['categories'] = Category.objects.filter(is_active=True).order_by('parent__name', 'name')
        ctx['q']        = self.request.GET.get('q', '')
        ctx['kind']     = self.request.GET.get('kind', '')
        ctx['stock']    = self.request.GET.get('stock', '')
        ctx['can_manage'] = can_manage_stock(self.request.user)
        return ctx


class ProductDetailView(_InventoryContextMixin, InventoryAccessMixin, DetailView):
    model = Product
    template_name = 'inventory/product_detail.html'
    context_object_name = 'product'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        product = self.object
        ctx['stock_items'] = (
            product.stock_items.select_related('location').order_by('location__name')
        )
        ctx['movements'] = (
            product.movements
            .select_related('location', 'actor')
            .order_by('-occurred_at')[:30]
        )
        ctx['batches'] = (
            StockBatch.objects.filter(stock_item__product=product)
            .select_related('stock_item__location', 'supplier')
            .order_by('expiry_date', '-received_at')[:30]
        )
        ctx['can_manage'] = can_manage_stock(self.request.user)
        return ctx


class ProductCreateView(_InventoryContextMixin, InventoryManagerMixin, CreateView):
    model = Product
    form_class = ProductForm
    template_name = 'inventory/product_form.html'

    def form_valid(self, form):
        form.instance.created_by = self.request.user
        messages.success(self.request, 'Product created.')
        return super().form_valid(form)


class ProductUpdateView(_InventoryContextMixin, InventoryManagerMixin, UpdateView):
    model = Product
    form_class = ProductForm
    template_name = 'inventory/product_form.html'

    def form_valid(self, form):
        messages.success(self.request, 'Product updated.')
        return super().form_valid(form)


# ════════════════════════════════════════════════════════════════════════════
# Stock movements
# ════════════════════════════════════════════════════════════════════════════

class ReceiveStockView(_InventoryContextMixin, InventoryManagerMixin, View):
    template_name = 'inventory/receive_stock.html'

    def get(self, request):
        form = ReceiveStockForm(initial={'product': request.GET.get('product')})
        return render(request, self.template_name, {'form': form})

    def post(self, request):
        form = ReceiveStockForm(request.POST)
        if not form.is_valid():
            return render(request, self.template_name, {'form': form})
        cd = form.cleaned_data
        try:
            services.receive_stock(
                product=cd['product'], location=cd['location'],
                quantity=cd['quantity'], cost_price=cd.get('cost_price'),
                supplier=cd.get('supplier'),
                batch_no=cd.get('batch_no') or '',
                expiry_date=cd.get('expiry_date'),
                notes=cd.get('notes') or '',
                actor=request.user,
                source_kind='manual_receive',
            )
            messages.success(request, f'Received {cd["quantity"]} {cd["product"].sku} at {cd["location"].code}.')
        except Exception as e:
            messages.error(request, f'Could not receive stock: {e}')
            return render(request, self.template_name, {'form': form})
        return redirect('inventory:product_detail', pk=cd['product'].pk)


class IssueStockView(_InventoryContextMixin, InventoryManagerMixin, View):
    template_name = 'inventory/issue_stock.html'

    def get(self, request):
        return render(request, self.template_name, {'form': IssueStockForm(
            initial={'product': request.GET.get('product')}
        )})

    def post(self, request):
        form = IssueStockForm(request.POST)
        if not form.is_valid():
            return render(request, self.template_name, {'form': form})
        cd = form.cleaned_data
        try:
            services.issue_stock(
                product=cd['product'], location=cd['location'],
                quantity=cd['quantity'],
                kind=cd['kind'], actor=request.user,
                notes=cd.get('notes') or '',
                source_kind='manual_issue',
                allow_oversell=True,
            )
            messages.success(request, f'Issued {cd["quantity"]} {cd["product"].sku} from {cd["location"].code}.')
        except Exception as e:
            messages.error(request, f'Could not issue stock: {e}')
            return render(request, self.template_name, {'form': form})
        return redirect('inventory:product_detail', pk=cd['product'].pk)


class TransferStockView(_InventoryContextMixin, InventoryManagerMixin, View):
    template_name = 'inventory/transfer_stock.html'

    def get(self, request):
        return render(request, self.template_name, {'form': TransferStockForm()})

    def post(self, request):
        form = TransferStockForm(request.POST)
        if not form.is_valid():
            return render(request, self.template_name, {'form': form})
        cd = form.cleaned_data
        try:
            services.transfer_stock(
                product=cd['product'],
                from_location=cd['from_location'],
                to_location=cd['to_location'],
                quantity=cd['quantity'],
                actor=request.user,
                notes=cd.get('notes') or '',
            )
            messages.success(
                request,
                f'Transferred {cd["quantity"]} {cd["product"].sku} from '
                f'{cd["from_location"].code} → {cd["to_location"].code}.',
            )
        except Exception as e:
            messages.error(request, f'Transfer failed: {e}')
            return render(request, self.template_name, {'form': form})
        return redirect('inventory:product_detail', pk=cd['product'].pk)


class MovementListView(_InventoryContextMixin, InventoryAccessMixin, ListView):
    template_name = 'inventory/movement_list.html'
    context_object_name = 'movements'
    paginate_by = 50

    def get_queryset(self):
        qs = (StockMovement.objects
              .select_related('product', 'location', 'actor')
              .order_by('-occurred_at'))
        kind = self.request.GET.get('kind', '').strip()
        loc  = self.request.GET.get('location', '').strip()
        prod = self.request.GET.get('product', '').strip()
        if kind:
            qs = qs.filter(kind=kind)
        if loc:
            qs = qs.filter(location_id=loc)
        if prod:
            qs = qs.filter(product_id=prod)
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['locations'] = Location.objects.filter(is_active=True).order_by('name')
        ctx['kind_choices'] = StockMovement.Kind.choices
        ctx['filters'] = {k: self.request.GET.get(k, '') for k in ['kind', 'location', 'product']}
        return ctx


# ════════════════════════════════════════════════════════════════════════════
# Locations / Categories / Suppliers (small CRUD)
# ════════════════════════════════════════════════════════════════════════════

class LocationListView(_InventoryContextMixin, InventoryAccessMixin, ListView):
    template_name = 'inventory/location_list.html'
    context_object_name = 'locations'

    def get_queryset(self):
        return (Location.objects
                .filter(is_active=True)
                .annotate(stock_lines=Count('stock_items'),
                          asset_count=Count('assets', distinct=True))
                .order_by('kind', 'name'))


class LocationDetailView(_InventoryContextMixin, InventoryAccessMixin, DetailView):
    model = Location
    template_name = 'inventory/location_detail.html'
    context_object_name = 'location'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['stock_items'] = (
            self.object.stock_items.select_related('product').order_by('product__name')
        )
        ctx['assets'] = self.object.assets.filter(is_active=True).order_by('name')
        ctx['can_manage'] = can_manage_stock(self.request.user)
        return ctx


class LocationCreateView(_InventoryContextMixin, InventoryManagerMixin, CreateView):
    model = Location
    form_class = LocationForm
    template_name = 'inventory/location_form.html'
    success_url = reverse_lazy('inventory:location_list')


class LocationUpdateView(_InventoryContextMixin, InventoryManagerMixin, UpdateView):
    model = Location
    form_class = LocationForm
    template_name = 'inventory/location_form.html'
    success_url = reverse_lazy('inventory:location_list')


class SupplierListView(_InventoryContextMixin, InventoryAccessMixin, ListView):
    template_name = 'inventory/supplier_list.html'
    context_object_name = 'suppliers'

    def get_queryset(self):
        return Supplier.objects.filter(is_active=True).order_by('name')


class SupplierCreateView(_InventoryContextMixin, InventoryManagerMixin, CreateView):
    model = Supplier
    form_class = SupplierForm
    template_name = 'inventory/supplier_form.html'
    success_url = reverse_lazy('inventory:supplier_list')


class SupplierUpdateView(_InventoryContextMixin, InventoryManagerMixin, UpdateView):
    model = Supplier
    form_class = SupplierForm
    template_name = 'inventory/supplier_form.html'
    success_url = reverse_lazy('inventory:supplier_list')


class CategoryListView(_InventoryContextMixin, InventoryAccessMixin, ListView):
    template_name = 'inventory/category_list.html'
    context_object_name = 'categories'

    def get_queryset(self):
        return Category.objects.filter(is_active=True).order_by('parent__name', 'name')


class CategoryCreateView(_InventoryContextMixin, InventoryManagerMixin, CreateView):
    model = Category
    form_class = CategoryForm
    template_name = 'inventory/category_form.html'
    success_url = reverse_lazy('inventory:category_list')


# ════════════════════════════════════════════════════════════════════════════
# Assets
# ════════════════════════════════════════════════════════════════════════════

class AssetListView(_InventoryContextMixin, InventoryAccessMixin, ListView):
    template_name = 'inventory/asset_list.html'
    context_object_name = 'assets'
    paginate_by = 30

    def get_queryset(self):
        qs = (Asset.objects
              .filter(is_active=True)
              .select_related('category', 'location', 'assigned_to')
              .order_by('name'))
        q = self.request.GET.get('q', '').strip()
        if q:
            qs = qs.filter(
                Q(tag__icontains=q) | Q(name__icontains=q) |
                Q(serial_number__icontains=q) | Q(model_number__icontains=q)
            )
        for k in ('status', 'condition', 'location'):
            v = self.request.GET.get(k, '').strip()
            if v:
                qs = qs.filter(**{f'{k}_id' if k == 'location' else k: v})
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['locations'] = Location.objects.filter(is_active=True).order_by('name')
        ctx['filters'] = {k: self.request.GET.get(k, '') for k in ['q', 'status', 'condition', 'location']}
        ctx['status_choices'] = Asset.Status.choices
        ctx['condition_choices'] = Asset.Condition.choices
        ctx['can_manage'] = can_manage_stock(self.request.user)
        return ctx


class AssetDetailView(_InventoryContextMixin, InventoryAccessMixin, DetailView):
    model = Asset
    template_name = 'inventory/asset_detail.html'
    context_object_name = 'asset'

    def get_object(self, queryset=None):
        obj = super().get_object(queryset)
        if not can_view_asset(self.request.user, obj):
            raise Http404
        return obj

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        a = self.object
        ctx['assignments']  = a.assignments.select_related('assignee', 'assigned_by').order_by('-assigned_at')
        ctx['maintenance']  = a.maintenance_records.order_by('-performed_on')
        ctx['events']       = a.events.select_related('actor').order_by('-created_at')[:30]
        ctx['can_manage']   = can_manage_stock(self.request.user)
        ctx['can_dispose']  = can_dispose_assets(self.request.user)
        ctx['assign_form']  = AssetAssignForm()
        ctx['return_form']  = AssetReturnForm()
        ctx['maint_form']   = AssetMaintenanceForm()
        return ctx


class AssetCreateView(_InventoryContextMixin, InventoryManagerMixin, CreateView):
    model = Asset
    form_class = AssetForm
    template_name = 'inventory/asset_form.html'

    def form_valid(self, form):
        form.instance.created_by = self.request.user
        resp = super().form_valid(form)
        AssetEvent.objects.create(
            asset=self.object, kind=AssetEvent.Kind.CREATED, actor=self.request.user,
            message='Asset registered.',
        )
        messages.success(self.request, f'Asset {self.object.tag} created.')
        return resp


class AssetUpdateView(_InventoryContextMixin, InventoryManagerMixin, UpdateView):
    model = Asset
    form_class = AssetForm
    template_name = 'inventory/asset_form.html'

    def form_valid(self, form):
        AssetEvent.objects.create(
            asset=form.instance, kind=AssetEvent.Kind.UPDATED,
            actor=self.request.user, message='Asset updated.',
        )
        return super().form_valid(form)


class AssetAssignView(_InventoryContextMixin, InventoryManagerMixin, View):
    def post(self, request, pk):
        asset = get_object_or_404(Asset, pk=pk)
        form = AssetAssignForm(request.POST)
        if not form.is_valid():
            messages.error(request, 'Could not assign — please check the form.')
            return redirect('inventory:asset_detail', pk=pk)
        try:
            services.assign_asset(
                asset, assignee=form.cleaned_data['assignee'],
                actor=request.user, purpose=form.cleaned_data.get('purpose') or '',
            )
            messages.success(request, f'Asset assigned to {form.cleaned_data["assignee"]}.')
        except Exception as e:
            messages.error(request, f'Could not assign: {e}')
        return redirect('inventory:asset_detail', pk=pk)


class AssetReturnView(_InventoryContextMixin, InventoryManagerMixin, View):
    def post(self, request, pk):
        asset = get_object_or_404(Asset, pk=pk)
        form = AssetReturnForm(request.POST)
        if not form.is_valid():
            messages.error(request, 'Invalid return form.')
            return redirect('inventory:asset_detail', pk=pk)
        try:
            res = services.return_asset(
                asset, actor=request.user,
                condition=form.cleaned_data.get('condition') or '',
                note=form.cleaned_data.get('note') or '',
            )
            if res:
                messages.success(request, 'Asset returned to store.')
            else:
                messages.info(request, 'Asset had no open assignment.')
        except Exception as e:
            messages.error(request, f'Could not return: {e}')
        return redirect('inventory:asset_detail', pk=pk)


class AssetMaintenanceView(_InventoryContextMixin, InventoryManagerMixin, View):
    def post(self, request, pk):
        asset = get_object_or_404(Asset, pk=pk)
        form = AssetMaintenanceForm(request.POST, request.FILES)
        if not form.is_valid():
            messages.error(request, 'Could not save maintenance record.')
            return redirect('inventory:asset_detail', pk=pk)
        cd = form.cleaned_data
        services.log_maintenance(
            asset, kind=cd['kind'], description=cd['description'],
            cost=cd.get('cost') or 0, performed_by=cd.get('performed_by') or '',
            performed_on=cd.get('performed_on'), next_due=cd.get('next_due'),
            actor=request.user, attachment=cd.get('attachment'),
        )
        messages.success(request, 'Maintenance record saved.')
        return redirect('inventory:asset_detail', pk=pk)


class AssetScrapView(_InventoryContextMixin, InventoryAccessMixin, View):
    def post(self, request, pk):
        if not can_dispose_assets(request.user):
            messages.error(request, 'Only the CEO/Admin can dispose of assets.')
            return redirect('inventory:asset_detail', pk=pk)
        asset = get_object_or_404(Asset, pk=pk)
        reason = (request.POST.get('reason') or '')[:500]
        try:
            services.scrap_asset(asset, actor=request.user, reason=reason)
            messages.success(request, f'Asset {asset.tag} disposed.')
        except Exception as e:
            messages.error(request, f'Disposal failed: {e}')
        return redirect('inventory:asset_detail', pk=pk)


# ════════════════════════════════════════════════════════════════════════════
# Stock requests
# ════════════════════════════════════════════════════════════════════════════

class StockRequestListView(_InventoryContextMixin, InventoryAccessMixin, ListView):
    template_name = 'inventory/stock_request_list.html'
    context_object_name = 'requests'
    paginate_by = 30

    def get_queryset(self):
        qs = (StockRequest.objects
              .select_related('requested_by', 'department', 'location')
              .order_by('-created_at'))
        if not can_manage_stock(self.request.user):
            qs = qs.filter(requested_by=self.request.user)
        status = self.request.GET.get('status', '').strip()
        if status:
            qs = qs.filter(status=status)
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['status_choices'] = StockRequest.Status.choices
        ctx['filters'] = {'status': self.request.GET.get('status', '')}
        ctx['can_manage'] = can_manage_stock(self.request.user)
        return ctx


class StockRequestCreateView(_InventoryContextMixin, InventoryAccessMixin, View):
    template_name = 'inventory/stock_request_form.html'

    def _eligible_products(self):
        """
        Products that can appear in the dropdown.

        Internal stock requests cover BOTH things being sold AND things used
        internally (printer paper, cleaning supplies, etc.), so we filter on
        is_active + kind=STOCKED rather than is_sellable. We also exclude
        anything with zero or negative on-hand stock — you can't request what
        isn't there.
        """
        from django.db.models import Sum, F, Value, DecimalField
        from django.db.models.functions import Coalesce
        from decimal import Decimal as D

        zero = Value(D('0'), output_field=DecimalField(max_digits=14, decimal_places=2))
        return (Product.objects
                .filter(is_active=True, kind=Product.Kind.STOCKED)
                .annotate(
                    on_hand_total=Coalesce(Sum('stock_items__quantity'), zero),
                    reserved_total=Coalesce(Sum('stock_items__reserved_quantity'), zero),
                )
                .annotate(available=F('on_hand_total') - F('reserved_total'))
                .filter(available__gt=0)
                .select_related('category')
                .order_by('name'))

    def get(self, request):
        return render(request, self.template_name, {
            'form': StockRequestForm(),
            'products': self._eligible_products(),
        })

    def post(self, request):
        form = StockRequestForm(request.POST)
        if not form.is_valid():
            return render(request, self.template_name, {
                'form': form,
                'products': self._eligible_products(),
            })

        # Build line list from POST. Field names match what the template
        # actually submits: line_product / line_qty / line_note (no [] —
        # Django's getlist returns multi-valued posts under the bare name).
        lines = []
        product_ids = request.POST.getlist('line_product')
        quantities  = request.POST.getlist('line_qty')
        notes       = request.POST.getlist('line_note')

        for idx, (pid, qty) in enumerate(zip(product_ids, quantities)):
            if not pid or not qty:
                continue
            try:
                qty_dec = Decimal(str(qty))
            except (InvalidOperation, ValueError):
                continue
            if qty_dec <= 0:
                continue
            note = notes[idx] if idx < len(notes) else ''
            lines.append({
                'product':  pid,
                'quantity': qty_dec,
                'note':     note,
            })

        if not lines:
            messages.error(request, 'Please add at least one product line.')
            return render(request, self.template_name, {
                'form': form,
                'products': self._eligible_products(),
            })

        try:
            sr = services.submit_stock_request(
                requested_by=request.user, lines=lines,
                department=form.cleaned_data.get('department'),
                location=form.cleaned_data.get('location'),
                purpose=form.cleaned_data.get('purpose') or '',
            )
            messages.success(request, f'Stock request {sr.reference} submitted.')
            return redirect('inventory:stock_request_detail', pk=sr.pk)
        except Exception as e:
            messages.error(request, f'Could not submit: {e}')
            return render(request, self.template_name, {
                'form': form,
                'products': self._eligible_products(),
            })


class StockRequestDetailView(_InventoryContextMixin, InventoryAccessMixin, DetailView):
    model = StockRequest
    template_name = 'inventory/stock_request_detail.html'
    context_object_name = 'sr'

    def get_object(self, queryset=None):
        obj = super().get_object(queryset)
        if not can_view_stock_request(self.request.user, obj):
            raise Http404
        return obj

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['lines'] = self.object.lines.select_related('product').all()
        ctx['can_manage'] = can_manage_stock(self.request.user)
        return ctx


class StockRequestIssueView(_InventoryContextMixin, InventoryManagerMixin, View):
    def post(self, request, pk):
        sr = get_object_or_404(StockRequest, pk=pk)
        location_id = request.POST.get('location') or None
        location = Location.objects.filter(pk=location_id).first() if location_id else None
        try:
            services.issue_stock_request(sr, actor=request.user, location=location)
            messages.success(request, f'Stock request {sr.reference} issued.')
        except Exception as e:
            messages.error(request, f'Could not issue: {e}')
            try:
                services.reroute_to_purchase(sr, actor=request.user)
                messages.info(request, 'Rerouted to a purchase request — finance has been notified.')
            except Exception as e2:
                messages.error(request, f'Reroute also failed: {e2}')
        return redirect('inventory:stock_request_detail', pk=pk)


class StockRequestRerouteView(_InventoryContextMixin, InventoryManagerMixin, View):
    def post(self, request, pk):
        sr = get_object_or_404(StockRequest, pk=pk)
        try:
            services.reroute_to_purchase(sr, actor=request.user)
            messages.success(request, f'Request {sr.reference} rerouted to a purchase.')
        except Exception as e:
            messages.error(request, f'Reroute failed: {e}')
        return redirect('inventory:stock_request_detail', pk=pk)


# ════════════════════════════════════════════════════════════════════════════
# QR scan endpoint
# ════════════════════════════════════════════════════════════════════════════

class QRResolveView(LoginRequiredMixin, View):
    """
    Universal QR landing page. Take a token (printed on a label) and
    redirect to the right detail page. Used by the mobile scanner.
    """
    def get(self, request, token):
        token = (token or '').strip()

        # 1. Asset
        asset = Asset.objects.filter(qr_token=token).first()
        if asset:
            return redirect('inventory:asset_detail', pk=asset.pk)

        # 2. Product (shelf-edge label)
        product = Product.objects.filter(qr_token=token).first()
        if product:
            return redirect('inventory:product_detail', pk=product.pk)

        # 3. StockItem (bin label)
        si = StockItem.objects.select_related('product', 'location').filter(qr_token=token).first()
        if si:
            return redirect('inventory:product_detail', pk=si.product.pk)

        # 4. Location (doorframe label)
        loc = Location.objects.filter(qr_token=token).first()
        if loc:
            return redirect('inventory:location_detail', pk=loc.pk)

        messages.error(request, 'Unknown QR code — this label may be from another system.')
        return redirect('inventory:dashboard')


class QRScanPageView(_InventoryContextMixin, InventoryAccessMixin, TemplateView):
    """Render the camera-based scanner UI (HTML5 + jsQR)."""
    template_name = 'inventory/scan.html'


# ════════════════════════════════════════════════════════════════════════════
# QR label printable
# ════════════════════════════════════════════════════════════════════════════

class QRLabelView(_InventoryContextMixin, InventoryAccessMixin, View):
    """
    Render a printable QR label (SVG) for a product / asset / location.
    Uses qrcode + Pillow if installed; falls back to an HTML page that
    renders the QR client-side via the qrcodejs CDN.
    """
    def get(self, request, kind, pk):
        target = self._resolve(kind, pk)
        if not target:
            raise Http404

        from django.urls import reverse
        token = target.qr_token
        try:
            scan_url = request.build_absolute_uri(reverse('inventory:qr_resolve', args=[token]))
        except Exception:
            scan_url = token

        ctx = {
            'kind': kind,
            'target': target,
            'token': token,
            'scan_url': scan_url,
            'label_text': self._label_text(kind, target),
        }
        return render(request, 'inventory/qr_label.html', ctx)

    def _resolve(self, kind, pk):
        try:
            return {
                'product':   lambda p: Product.objects.get(pk=p),
                'asset':     lambda p: Asset.objects.get(pk=p),
                'location':  lambda p: Location.objects.get(pk=p),
                'stockitem': lambda p: StockItem.objects.select_related('product', 'location').get(pk=p),
            }[kind](pk)
        except Exception:
            return None

    def _label_text(self, kind, t):
        if kind == 'product':   return f'{t.sku} · {t.name}'
        if kind == 'asset':     return f'{t.tag} · {t.name}'
        if kind == 'location':  return f'{t.code} · {t.name}'
        if kind == 'stockitem': return f'{t.product.sku} · {t.location.code}'
        return ''


# ════════════════════════════════════════════════════════════════════════════
# Stock-take
# ════════════════════════════════════════════════════════════════════════════

class StockTakeListView(_InventoryContextMixin, InventoryManagerMixin, ListView):
    template_name = 'inventory/stocktake_list.html'
    context_object_name = 'takes'
    paginate_by = 20

    def get_queryset(self):
        return StockTake.objects.select_related('location', 'started_by').order_by('-started_at')


class StockTakeCreateView(_InventoryContextMixin, InventoryManagerMixin, View):
    template_name = 'inventory/stocktake_form.html'

    def get(self, request):
        return render(request, self.template_name, {'form': StockTakeForm()})

    def post(self, request):
        form = StockTakeForm(request.POST)
        if not form.is_valid():
            return render(request, self.template_name, {'form': form})
        st = form.save(commit=False)
        st.started_by = request.user
        st.status = StockTake.Status.IN_PROGRESS
        st.save()

        # Pre-fill lines from current stock at this location
        for si in (StockItem.objects
                   .filter(location=st.location, product__kind=Product.Kind.STOCKED)
                   .select_related('product')):
            StockTakeLine.objects.create(
                stock_take=st, product=si.product, expected=si.quantity,
            )
        messages.success(request, f'Stock-take {st.reference} started — count items now.')
        return redirect('inventory:stocktake_detail', pk=st.pk)


class StockTakeDetailView(_InventoryContextMixin, InventoryManagerMixin, DetailView):
    model = StockTake
    template_name = 'inventory/stocktake_detail.html'
    context_object_name = 'st'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['lines'] = self.object.lines.select_related('product').order_by('product__name')
        return ctx

    def post(self, request, pk):
        st = self.get_object()
        if 'count' in request.POST:
            for line in st.lines.all():
                key = f'counted_{line.pk}'
                if key in request.POST and request.POST[key].strip():
                    try:
                        line.counted = Decimal(request.POST[key])
                        line.save(update_fields=['counted'])
                    except (InvalidOperation, ValueError):
                        continue
            messages.success(request, 'Counts saved.')
            return redirect('inventory:stocktake_detail', pk=pk)
        if 'finalise' in request.POST:
            try:
                services.finalise_stock_take(st, actor=request.user)
                messages.success(request, 'Stock-take finalised — adjustments posted.')
            except Exception as e:
                messages.error(request, f'Finalise failed: {e}')
        return redirect('inventory:stocktake_detail', pk=pk)


# ════════════════════════════════════════════════════════════════════════════
# Reports
# ════════════════════════════════════════════════════════════════════════════

class StockReportView(_InventoryContextMixin, InventoryManagerMixin, TemplateView):
    template_name = 'inventory/report_stock.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        items = (StockItem.objects
                 .select_related('product', 'location')
                 .filter(product__is_active=True, product__kind=Product.Kind.STOCKED)
                 .order_by('product__name', 'location__name'))
        total_value = Decimal('0.00')
        rows = []
        for si in items:
            value = (si.quantity or Decimal('0')) * (si.product.cost_price or Decimal('0'))
            total_value += value
            rows.append((si, value))
        ctx['rows'] = rows
        ctx['total_value'] = total_value
        return ctx


class AssetReportView(_InventoryContextMixin, InventoryManagerMixin, TemplateView):
    template_name = 'inventory/report_assets.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        assets = (Asset.objects
                  .filter(is_active=True)
                  .select_related('location', 'category', 'assigned_to')
                  .order_by('category__name', 'name'))
        total_cost = sum((a.purchase_cost or Decimal('0') for a in assets), Decimal('0.00'))
        total_book = sum((a.book_value for a in assets), Decimal('0.00'))
        ctx['assets']     = assets
        ctx['total_cost'] = total_cost
        ctx['total_book'] = total_book
        return ctx


# ════════════════════════════════════════════════════════════════════════════
# Access control admin
# ════════════════════════════════════════════════════════════════════════════

class _AdminMixin(LoginRequiredMixin):
    """Gate for the access-control admin views."""
    def dispatch(self, request, *args, **kwargs):
        from .permissions import can_administer_access
        if request.user.is_authenticated and not can_administer_access(request.user):
            raise PermissionDenied("You don't have rights to administer inventory access.")
        return super().dispatch(request, *args, **kwargs)


class AccessControlListView(_InventoryContextMixin, _AdminMixin, View):
    """
    GET  /inventory/access/
        Lists every grant grouped by scope (departments first, then users).
        Has an inline form to add a new grant.
    POST /inventory/access/
        Adds a new grant from the form. Redirects back to the list.
    """
    template_name = 'inventory/access_list.html'

    def get(self, request):
        from .models import InventoryAccessGrant
        from .access import Module, Level

        Department = self._department_model()
        UserModel  = get_user_model()

        dept_grants = (InventoryAccessGrant.objects
                       .filter(department__isnull=False, is_active=True)
                       .select_related('department', 'granted_by')
                       .order_by('department__name', 'module'))
        user_grants = (InventoryAccessGrant.objects
                       .filter(user__isnull=False, is_active=True)
                       .select_related('user', 'granted_by')
                       .order_by('user__username', 'module'))

        ctx = {
            'dept_grants':  dept_grants,
            'user_grants':  user_grants,
            'departments':  Department.objects.all().order_by('name') if Department else [],
            'users':        UserModel.objects.filter(is_active=True).order_by(
                              'first_name', 'last_name', 'username'),
            'module_choices': Module.CHOICES,
            'level_choices':  Level.CHOICES,
        }
        return render(request, self.template_name, ctx)

    def post(self, request):
        from .models import InventoryAccessGrant
        from .access import Module, Level, invalidate_cache

        scope    = request.POST.get('scope') or ''
        target   = request.POST.get('target') or ''
        module   = (request.POST.get('module') or '').strip()
        level    = (request.POST.get('level') or 'view').strip()
        notes    = (request.POST.get('notes') or '').strip()[:200]

        if not target or scope not in {'user', 'department'}:
            messages.error(request, 'Pick a scope and a target.')
            return redirect('inventory:access_list')
        if module not in Module.ALL:
            messages.error(request, 'Unknown module.')
            return redirect('inventory:access_list')
        if level not in {'view', 'operate', 'manage'}:
            messages.error(request, 'Unknown level.')
            return redirect('inventory:access_list')

        # Existing rows for the same scope+module are upserted (level may
        # change). The unique constraint enforces this at the DB level too.
        defaults = {
            'level':       level,
            'notes':       notes,
            'is_active':   True,
            'granted_by':  request.user,
        }
        if scope == 'user':
            obj, created = InventoryAccessGrant.objects.update_or_create(
                user_id=target, module=module, defaults=defaults,
            )
        else:
            obj, created = InventoryAccessGrant.objects.update_or_create(
                department_id=target, module=module, defaults=defaults,
            )

        invalidate_cache(request.user)  # in case admin granted to themselves
        verb = 'created' if created else 'updated'
        messages.success(request, f'Grant {verb}: {obj}')
        return redirect('inventory:access_list')

    @staticmethod
    def _department_model():
        try:
            from django.apps import apps as django_apps
            return django_apps.get_model('organization', 'Department')
        except Exception:
            return None


class AccessControlRevokeView(_AdminMixin, View):
    """POST /inventory/access/<pk>/revoke/  — soft-delete a grant."""
    def post(self, request, pk):
        from .models import InventoryAccessGrant
        try:
            grant = InventoryAccessGrant.objects.get(pk=pk)
        except InventoryAccessGrant.DoesNotExist:
            messages.error(request, 'Grant not found.')
            return redirect('inventory:access_list')
        grant.is_active = False
        grant.save(update_fields=['is_active', 'updated_at'])
        messages.success(request, f'Revoked: {grant}')
        return redirect('inventory:access_list')


# ════════════════════════════════════════════════════════════════════════════
# Catalog generator + best sellers
# ════════════════════════════════════════════════════════════════════════════

class CatalogBuilderView(_InventoryContextMixin, InventoryAccessMixin, TemplateView):
    """
    GET /inventory/catalog/
        Renders the builder page — pick template, columns, theme — and a
        live preview iframe.
    """
    template_name = 'inventory/catalog_builder.html'

    TEMPLATES = [
        {'key': 'grid',     'name': 'Grid',
         'description': 'Photo cards with name, SKU, and price — best for product showcases.'},
        {'key': 'magazine', 'name': 'Magazine',
         'description': 'Editorial layout with hero image and side columns. Great for premium products.'},
        {'key': 'pricelist','name': 'Price list',
         'description': 'Compact table — many items per page, ideal for B2B handouts.'},
    ]

    THEMES = [
        {'key': 'midnight', 'name': 'Midnight',  'primary': '#0f172a', 'accent': '#2563eb'},
        {'key': 'emerald',  'name': 'Emerald',   'primary': '#064e3b', 'accent': '#10b981'},
        {'key': 'sunset',   'name': 'Sunset',    'primary': '#7c2d12', 'accent': '#f59e0b'},
        {'key': 'rose',     'name': 'Rose',      'primary': '#831843', 'accent': '#e11d48'},
        {'key': 'mono',     'name': 'Mono',      'primary': '#111827', 'accent': '#374151'},
    ]

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['templates'] = self.TEMPLATES
        ctx['themes']    = self.THEMES
        ctx['categories'] = Category.objects.filter(is_active=True).order_by('name')
        return ctx


class CatalogRenderView(_InventoryContextMixin, InventoryAccessMixin, View):
    """
    GET /inventory/catalog/render/?template=grid&theme=midnight&...
        Renders the catalog as standalone HTML — used both as the iframe
        preview and as the printable / shareable artifact.

    Query parameters:
        template  : grid | magazine | pricelist  (default: grid)
        theme     : midnight | emerald | sunset | rose | mono (default: midnight)
        title     : cover title (default: "Product Catalog")
        subtitle  : cover subtitle / tagline
        footer    : footer line (e.g. company contact info)
        category  : limit to a single category (UUID or empty for all)
        show_sku  : 1 | 0
        show_price: 1 | 0
        show_stock: 1 | 0
        show_cost : 1 | 0  (manager-only)
        currency  : override currency display
    """
    def get(self, request):
        template_key = request.GET.get('template', 'grid')
        if template_key not in {'grid', 'magazine', 'pricelist'}:
            template_key = 'grid'
        theme_key = request.GET.get('theme', 'midnight')

        theme = next((t for t in CatalogBuilderView.THEMES
                      if t['key'] == theme_key),
                     CatalogBuilderView.THEMES[0])

        # Filter products: sellable only by default
        qs = (Product.objects
              .filter(is_active=True, is_sellable=True)
              .select_related('category')
              .order_by('category__name', 'name'))

        category_id = (request.GET.get('category') or '').strip()
        if category_id:
            qs = qs.filter(category_id=category_id)

        # Optional: top N best sellers
        top_n = (request.GET.get('top') or '').strip()
        if top_n.isdigit():
            qs = self._sort_by_sales(qs)[:int(top_n)]

        # Field-level access — non-managers can never see cost in catalog
        from .access import can_manage as _cm
        is_manager = _cm(request.user, 'products')

        opts = {
            'title':      request.GET.get('title') or 'Product Catalog',
            'subtitle':   request.GET.get('subtitle') or '',
            'footer':     request.GET.get('footer') or '',
            'show_sku':   request.GET.get('show_sku',   '1') == '1',
            'show_price': request.GET.get('show_price', '1') == '1',
            'show_stock': request.GET.get('show_stock', '0') == '1',
            'show_cost':  is_manager and request.GET.get('show_cost', '0') == '1',
            'currency':   request.GET.get('currency', 'GMD'),
        }

        ctx = {
            'products': list(qs),
            'theme':    theme,
            'opts':     opts,
            'site_url': getattr(settings, 'SITE_URL', '').rstrip('/'),
            'now':      timezone.now(),
        }
        return render(request, f'inventory/catalog/_{template_key}.html', ctx)

    def _sort_by_sales(self, qs):
        """Order products by total quantity sold over the last 90 days."""
        from datetime import timedelta
        cutoff = timezone.now() - timedelta(days=90)
        return qs.annotate(
            sold=Sum(
                'order_lines__quantity',
                filter=Q(order_lines__order__status='fulfilled',
                         order_lines__order__fulfilled_at__gte=cutoff),
            )
        ).order_by(F('sold').desc(nulls_last=True), 'name')


class BestSellersView(_InventoryContextMixin, InventoryAccessMixin, TemplateView):
    """
    GET /inventory/best-sellers/
        Builder for downloadable best-sellers card.
    """
    template_name = 'inventory/best_sellers.html'

    def get_context_data(self, **kwargs):
        from datetime import timedelta
        ctx = super().get_context_data(**kwargs)
        cutoff = timezone.now() - timedelta(days=90)

        rows = (Product.objects
                .filter(is_active=True, is_sellable=True)
                .annotate(
                    sold=Sum(
                        'order_lines__quantity',
                        filter=Q(order_lines__order__status='fulfilled',
                                 order_lines__order__fulfilled_at__gte=cutoff),
                    ),
                    revenue=Sum(
                        F('order_lines__quantity') * F('order_lines__unit_price'),
                        filter=Q(order_lines__order__status='fulfilled',
                                 order_lines__order__fulfilled_at__gte=cutoff),
                    ),
                )
                .filter(sold__gt=0)
                .order_by('-sold')[:10])
        ctx['rows'] = rows
        ctx['period_days'] = 90
        ctx['themes'] = CatalogBuilderView.THEMES
        return ctx


class BestSellersCardView(_InventoryContextMixin, InventoryAccessMixin, View):
    """
    GET /inventory/best-sellers/card/?theme=midnight&top=5
        Renders the standalone shareable card (HTML — user prints to PDF
        or uses html2canvas to save as PNG client-side).
    """
    def get(self, request):
        from datetime import timedelta
        theme_key = request.GET.get('theme', 'midnight')
        top = int(request.GET.get('top', '5') or '5')
        top = max(3, min(top, 10))

        theme = next((t for t in CatalogBuilderView.THEMES
                      if t['key'] == theme_key),
                     CatalogBuilderView.THEMES[0])

        cutoff = timezone.now() - timedelta(days=90)
        rows = (Product.objects
                .filter(is_active=True, is_sellable=True)
                .annotate(
                    sold=Sum(
                        'order_lines__quantity',
                        filter=Q(order_lines__order__status='fulfilled',
                                 order_lines__order__fulfilled_at__gte=cutoff),
                    ),
                )
                .filter(sold__gt=0)
                .order_by('-sold')[:top])

        return render(request, 'inventory/catalog/_bestseller_card.html', {
            'rows':  rows,
            'theme': theme,
            'title': request.GET.get('title') or 'Top Sellers',
            'subtitle': request.GET.get('subtitle') or 'Last 90 days',
            'footer': request.GET.get('footer') or '',
            'now':    timezone.now(),
        })
