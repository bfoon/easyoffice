"""
apps/orders/views.py
────────────────────
Internal HTML views — list, detail, create, fulfill, cancel.

These are agent-facing. The public API lives in apps/orders/api/views.py.
"""
from decimal import Decimal
from django.contrib import messages
from django.db.models import Q
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse, reverse_lazy
from django.views.generic import ListView, DetailView, View, TemplateView

from .models import SalesOrder, SalesOrderItem, OrderEvent, OrderStatus, OrderSource
from .forms import OrderHeaderForm, OrderItemForm, CancelOrderForm, AttachCustomerForm
from .permissions import (
    OrdersAccessMixin, can_fulfill_order, can_cancel_order,
)
from . import services


# ── Dashboard (lightweight overview) ────────────────────────────────────────

class OrdersDashboardView(OrdersAccessMixin, TemplateView):
    template_name = 'orders/dashboard.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        qs = SalesOrder.objects.all()
        ctx['count_new']            = qs.filter(status=OrderStatus.NEW).count()
        ctx['count_confirmed']      = qs.filter(status=OrderStatus.CONFIRMED).count()
        ctx['count_in_fulfillment'] = qs.filter(status=OrderStatus.IN_FULFILLMENT).count()
        ctx['count_fulfilled']      = qs.filter(status=OrderStatus.FULFILLED).count()
        ctx['count_cancelled']      = qs.filter(status=OrderStatus.CANCELLED).count()
        ctx['recent_orders'] = qs.select_related('customer').order_by('-created_at')[:15]
        ctx['source_choices'] = OrderSource.choices
        ctx['status_choices'] = OrderStatus.choices
        return ctx


# ── List ───────────────────────────────────────────────────────────────────

class OrderListView(OrdersAccessMixin, ListView):
    template_name        = 'orders/list.html'
    context_object_name  = 'orders'
    paginate_by          = 25
    model                = SalesOrder

    def get_queryset(self):
        qs = SalesOrder.objects.select_related('customer', 'invoice', 'proforma', 'delivery_note')
        gp = self.request.GET
        if status := gp.get('status'):
            qs = qs.filter(status=status)
        if source := gp.get('source'):
            qs = qs.filter(source=source)
        if q := gp.get('q'):
            qs = qs.filter(
                Q(order_no__icontains=q) |
                Q(customer__full_name__icontains=q) |
                Q(contact_name__icontains=q) |
                Q(contact_phone__icontains=q) |
                Q(external_ref__icontains=q)
            )
        return qs.order_by('-created_at')

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['status_choices'] = OrderStatus.choices
        ctx['source_choices'] = OrderSource.choices
        ctx['current_status'] = self.request.GET.get('status', '')
        ctx['current_source'] = self.request.GET.get('source', '')
        ctx['q']              = self.request.GET.get('q', '')
        return ctx


# ── Create (agent enters phone / walk-in order) ────────────────────────────

class OrderCreateView(OrdersAccessMixin, TemplateView):
    """
    Single-page form: header + line items.

    Items are submitted as repeating fields:
        items-0-description, items-0-quantity, items-0-unit_price
        items-1-description, items-1-quantity, items-1-unit_price
        ...

    Empty rows are silently dropped.
    """
    template_name = 'orders/create.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['header_form'] = OrderHeaderForm(initial={'currency': 'GMD'})
        ctx['source_choices'] = OrderSource.choices
        return ctx

    def post(self, request, *args, **kwargs):
        header = OrderHeaderForm(request.POST)
        if not header.is_valid():
            messages.error(request, 'Please correct the errors below.')
            return self.render_to_response({
                'header_form': header,
                'source_choices': OrderSource.choices,
            })

        # Collect items by index
        items = []
        i = 0
        while True:
            desc = request.POST.get(f'items-{i}-description', '').strip()
            qty  = request.POST.get(f'items-{i}-quantity', '').strip()
            up   = request.POST.get(f'items-{i}-unit_price', '').strip()
            if not (desc or qty or up):
                # Allow holes (i=0 missing then i=1 present) — keep scanning
                # for a few more rows in case the user reordered.
                if i > 25:
                    break
                i += 1
                continue
            if not desc:
                # row partially filled — skip
                i += 1
                continue
            try:
                items.append({
                    'description': desc,
                    'quantity':    Decimal(qty or '1'),
                    'unit_price':  Decimal(up or '0'),
                })
            except Exception:
                messages.error(request, f'Invalid number on row {i + 1}.')
                return self.render_to_response({
                    'header_form': header,
                    'source_choices': OrderSource.choices,
                })
            i += 1

        if not items:
            messages.error(request, 'Add at least one line item.')
            return self.render_to_response({
                'header_form': header,
                'source_choices': OrderSource.choices,
            })

        source = request.POST.get('source', OrderSource.PHONE)
        if source not in {s for s, _ in OrderSource.choices}:
            source = OrderSource.PHONE

        # Build payload from form data + customer FK
        cleaned = header.cleaned_data
        payload = {
            'customer_id':      cleaned['customer'].pk if cleaned.get('customer') else None,
            'contact_name':     cleaned.get('contact_name', ''),
            'contact_phone':    cleaned.get('contact_phone', ''),
            'contact_email':    cleaned.get('contact_email', ''),
            'delivery_address': cleaned.get('delivery_address', ''),
            'notes':            cleaned.get('notes', ''),
            'currency':         cleaned.get('currency', 'GMD'),
            'tax_rate':         cleaned.get('tax_rate', 0),
            'discount_amount':  cleaned.get('discount_amount', 0),
            'items':            items,
        }
        order = services.create_order_from_payload(payload, source=source, actor=request.user)
        messages.success(request, f'Order {order.order_no} created.')
        return redirect('orders:order_detail', pk=order.pk)


# ── Detail ─────────────────────────────────────────────────────────────────

class OrderDetailView(OrdersAccessMixin, DetailView):
    model               = SalesOrder
    template_name       = 'orders/detail.html'
    context_object_name = 'order'

    def get_queryset(self):
        return SalesOrder.objects.select_related(
            'customer', 'proforma', 'invoice', 'delivery_note',
            'created_by', 'fulfilled_by',
        ).prefetch_related('items', 'events__actor')

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['can_fulfill']        = can_fulfill_order(self.request.user, self.object)
        ctx['can_cancel']         = can_cancel_order(self.request.user, self.object)
        ctx['attach_customer_form'] = AttachCustomerForm()
        ctx['cancel_form']        = CancelOrderForm()
        return ctx


# ── Action endpoints ───────────────────────────────────────────────────────

class OrderFulfillView(OrdersAccessMixin, View):
    """POST /<pk>/fulfill/  → builds Proforma → Invoice → Delivery Note chain."""
    def post(self, request, pk):
        order = get_object_or_404(SalesOrder, pk=pk)
        if not can_fulfill_order(request.user, order):
            messages.error(request, "You don't have permission to fulfill orders.")
            return redirect('orders:order_detail', pk=order.pk)
        try:
            # fulfill_order does its work on a row it locked via
            # select_for_update — the local `order` we passed in is stale
            # by the time the function returns. Capture the refreshed
            # instance and re-fetch the FKs so the success message
            # reflects the new state.
            order = services.fulfill_order(order, request.user)
        except ValueError as e:
            messages.error(request, str(e))
            return redirect('orders:order_detail', pk=order.pk)
        except Exception as e:
            messages.error(request, f'Could not fulfill order: {e}')
            return redirect('orders:order_detail', pk=order.pk)

        # Re-load with the doc FKs joined in case the returned instance
        # came from select_for_update (which can leave related-object
        # caches unfilled).
        order = (
            SalesOrder.objects
            .select_related('proforma', 'invoice', 'delivery_note')
            .get(pk=order.pk)
        )

        # Build a defensive list of generated doc numbers — every doc
        # SHOULD be present after a successful fulfillment, but we don't
        # want a missing one to crash the success path.
        generated = [
            d.number for d in (order.proforma, order.invoice, order.delivery_note)
            if d and getattr(d, 'number', None)
        ]
        if generated:
            messages.success(
                request,
                f'Order fulfilled. Generated {", ".join(generated)}.',
            )
        else:
            messages.success(request, 'Order fulfilled.')
        return redirect('orders:order_detail', pk=order.pk)


class OrderCancelView(OrdersAccessMixin, View):
    def post(self, request, pk):
        order = get_object_or_404(SalesOrder, pk=pk)
        if not can_cancel_order(request.user, order):
            messages.error(request, "You don't have permission to cancel orders.")
            return redirect('orders:order_detail', pk=order.pk)
        form = CancelOrderForm(request.POST)
        reason = form.cleaned_data.get('reason', '') if form.is_valid() else ''
        try:
            services.cancel_order(order, request.user, reason=reason)
        except ValueError as e:
            messages.error(request, str(e))
            return redirect('orders:order_detail', pk=order.pk)
        messages.success(request, f'Order {order.order_no} cancelled.')
        return redirect('orders:order_detail', pk=order.pk)


class OrderAttachCustomerView(OrdersAccessMixin, View):
    def post(self, request, pk):
        order = get_object_or_404(SalesOrder, pk=pk)
        form = AttachCustomerForm(request.POST)
        if not form.is_valid():
            messages.error(request, 'Pick a customer to attach.')
            return redirect('orders:order_detail', pk=order.pk)
        services.attach_customer(order, form.cleaned_data['customer'], request.user)
        messages.success(request, 'Customer attached.')
        return redirect('orders:order_detail', pk=order.pk)


class OrderConfirmView(OrdersAccessMixin, View):
    """Soft-confirm: just flips status NEW → CONFIRMED. Useful for triage."""
    def post(self, request, pk):
        order = get_object_or_404(SalesOrder, pk=pk)
        if order.status != OrderStatus.NEW:
            messages.warning(request, f'Order is already {order.get_status_display()}.')
            return redirect('orders:order_detail', pk=order.pk)
        order.status = OrderStatus.CONFIRMED
        order.save(update_fields=['status', 'updated_at'])
        OrderEvent.objects.create(
            order=order, kind=OrderEvent.Kind.CONFIRMED,
            actor=request.user, message='Order confirmed by agent',
        )
        messages.success(request, 'Order confirmed.')
        return redirect('orders:order_detail', pk=order.pk)