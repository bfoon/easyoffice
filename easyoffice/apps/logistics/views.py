"""
apps/logistics/views.py
=======================

Staff-side logistics console (logged-in). Dispatch, assign, track.

  * LogisticsDashboardView   /logistics/
  * ShipmentListView         /logistics/shipments/
  * ShipmentCreateView       /logistics/shipments/new/
  * ShipmentDetailView       /logistics/shipments/<pk>/
  * ShipmentAssignView       /logistics/shipments/<pk>/assign/   (driver+vehicle)
  * ShipmentDispatchView     /logistics/shipments/<pk>/dispatch/
  * ShipmentCancelView       /logistics/shipments/<pk>/cancel/
  * ShipmentTrackView        /logistics/shipments/<pk>/track/     (live map)
  * ShipmentTrackDataView    /logistics/shipments/<pk>/track/data/ (JSON pings)
  * VehicleListView / VehicleSaveView
  * DriverListView / DriverSaveView
  * DriverLinkActionView     issue / activate / deactivate / regenerate links
"""
from __future__ import annotations

import logging

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.generic import TemplateView, View

from apps.core.models import User
from apps.logistics.models import (
    Vehicle, Driver, DriverPortalToken, Shipment, ShipmentItem,
)

logger = logging.getLogger(__name__)


def _is_logistics_staff(user):
    return user.is_superuser or user.groups.filter(
        name__in=['Logistics', 'Admin', 'Office Manager', 'CEO'],
    ).exists()


def _gen_reference():
    try:
        import shortuuid
        return f'SHP-{shortuuid.ShortUUID().random(length=6).upper()}'
    except ImportError:
        import random, string
        return 'SHP-' + ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))


class _StaffMixin(LoginRequiredMixin):
    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return super().dispatch(request, *args, **kwargs)
        if not _is_logistics_staff(request.user):
            messages.error(request, 'Logistics staff only.')
            return redirect('dashboard')
        return super().dispatch(request, *args, **kwargs)


# ─────────────────────────────────────────────────────────────────────────────
# Dashboard
# ─────────────────────────────────────────────────────────────────────────────

class LogisticsDashboardView(_StaffMixin, TemplateView):
    template_name = 'logistics/dashboard.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        shipments = Shipment.objects.all()
        ctx.update({
            'in_transit': shipments.filter(status__in=['dispatched', 'en_route', 'arrived'])
                .select_related('driver', 'vehicle').order_by('-dispatched_at')[:12],
            'ready': shipments.filter(status='ready').select_related('driver', 'vehicle')
                .order_by('scheduled_for')[:12],
            'counts': {
                'ready': shipments.filter(status='ready').count(),
                'in_transit': shipments.filter(status__in=['dispatched', 'en_route', 'arrived']).count(),
                'delivered_today': shipments.filter(
                    status='delivered', delivered_at__date=timezone.now().date()).count(),
                'failed': shipments.filter(status='failed').count(),
            },
            'fleet': {
                'company': Vehicle.objects.filter(ownership='company').count(),
                'hired': Vehicle.objects.filter(ownership='hired').count(),
                'on_delivery': Vehicle.objects.filter(status='on_delivery').count(),
            },
            'active_drivers': Driver.objects.filter(is_active=True).count(),
        })
        return ctx


# ─────────────────────────────────────────────────────────────────────────────
# Shipments
# ─────────────────────────────────────────────────────────────────────────────

class ShipmentListView(_StaffMixin, TemplateView):
    template_name = 'logistics/shipment_list.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        qs = Shipment.objects.select_related('driver', 'vehicle').all()
        status = self.request.GET.get('status')
        if status:
            qs = qs.filter(status=status)
        ctx['shipments'] = qs.order_by('-created_at')[:200]
        ctx['status_choices'] = Shipment.Status.choices
        ctx['current_status'] = status or ''
        return ctx


class ShipmentCreateView(_StaffMixin, View):
    def get(self, request):
        return render(request, 'logistics/shipment_form.html', self._form_ctx())

    def post(self, request):
        customer = (request.POST.get('customer_name') or '').strip()
        dest = (request.POST.get('destination_address') or '').strip()
        if not customer or not dest:
            messages.error(request, 'Customer name and destination address are required.')
            return render(request, 'logistics/shipment_form.html', self._form_ctx())

        shipment = Shipment.objects.create(
            reference=_gen_reference(),
            status=Shipment.Status.DRAFT,
            customer_name=customer[:200],
            contact_name=(request.POST.get('contact_name') or '').strip()[:160],
            contact_phone=(request.POST.get('contact_phone') or '').strip()[:40],
            contact_email=(request.POST.get('contact_email') or '').strip(),
            origin_label=(request.POST.get('origin_label') or 'Office').strip()[:200],
            destination_address=dest,
            destination_lat=_dec(request.POST.get('destination_lat')),
            destination_lng=_dec(request.POST.get('destination_lng')),
            plus_code=(request.POST.get('plus_code') or '').strip()[:32],
            instructions=(request.POST.get('instructions') or '').strip(),
            scheduled_for=_dt(request.POST.get('scheduled_for')),
            created_by=request.user,
        )
        self._link_order_invoice(request, shipment)
        self._save_items(request, shipment)
        shipment.add_event(Shipment.Status.DRAFT, actor=request.user, note='Shipment created')
        messages.success(request, f'Shipment {shipment.reference} created.')
        return redirect('logistics_shipment_detail', pk=shipment.pk)

    def _form_ctx(self):
        ctx = {
            'vehicles': Vehicle.objects.exclude(status='retired').order_by('label'),
            'drivers': Driver.objects.filter(is_active=True).order_by('full_name'),
        }
        # Optional pickers for linking — kept defensive in case apps differ.
        try:
            from apps.orders.models import SalesOrder
            ctx["orders"] = SalesOrder.objects.order_by('-created_at')[:100]
        except Exception:
            ctx['orders'] = []
        try:
            from apps.finance.models import IncomingPaymentRequest
            ctx['invoices'] = IncomingPaymentRequest.objects.order_by('-created_at')[:100]
        except Exception:
            ctx['invoices'] = []
        return ctx

    def _link_order_invoice(self, request, shipment):
        oid = request.POST.get('purchase_order')
        iid = request.POST.get('invoice')
        if oid:
            try:
                from apps.orders.models import SalesOrder
                shipment.purchase_order = SalesOrder.objects.filter(pk=oid).first()
            except Exception:
                pass
        if iid:
            try:
                from apps.finance.models import IncomingPaymentRequest
                shipment.invoice = IncomingPaymentRequest.objects.filter(pk=iid).first()
            except Exception:
                pass
        if oid or iid:
            shipment.save(update_fields=['purchase_order', 'invoice', 'updated_at'])

    def _save_items(self, request, shipment):
        descs = request.POST.getlist('item_description')
        qtys = request.POST.getlist('item_quantity')
        units = request.POST.getlist('item_unit')
        for i, d in enumerate(descs):
            d = (d or '').strip()
            if not d:
                continue
            ShipmentItem.objects.create(
                shipment=shipment, description=d[:300], position=i,
                quantity=_dec(qtys[i] if i < len(qtys) else None) or 1,
                unit=(units[i] if i < len(units) else '').strip()[:30],
            )


class ShipmentDetailView(_StaffMixin, TemplateView):
    template_name = 'logistics/shipment_detail.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        shipment = get_object_or_404(
            Shipment.objects.select_related('driver', 'vehicle', 'purchase_order', 'invoice'),
            pk=kwargs['pk'],
        )
        ctx.update({
            'shipment': shipment,
            'items': shipment.items.all(),
            'events': shipment.events.select_related('actor').all(),
            'proof': getattr(shipment, 'proof', None),
            'vehicles': Vehicle.objects.exclude(status='retired').order_by('label'),
            'drivers': Driver.objects.filter(is_active=True).order_by('full_name'),
            'status_choices': Shipment.Status.choices,
            'latest_ping': shipment.latest_ping,
        })
        return ctx


class ShipmentAssignView(_StaffMixin, View):
    http_method_names = ['post']

    def post(self, request, pk):
        shipment = get_object_or_404(Shipment, pk=pk)
        driver_id = request.POST.get('driver')
        vehicle_id = request.POST.get('vehicle')
        shipment.driver = Driver.objects.filter(pk=driver_id).first() if driver_id else None
        shipment.vehicle = Vehicle.objects.filter(pk=vehicle_id).first() if vehicle_id else None
        if shipment.status == Shipment.Status.DRAFT and shipment.driver and shipment.vehicle:
            shipment.status = Shipment.Status.READY
        shipment.save(update_fields=['driver', 'vehicle', 'status', 'updated_at'])
        messages.success(request, 'Driver and vehicle assigned.')
        return redirect('logistics_shipment_detail', pk=pk)


class ShipmentDispatchView(_StaffMixin, View):
    http_method_names = ['post']

    def post(self, request, pk):
        shipment = get_object_or_404(Shipment, pk=pk)
        if not shipment.driver or not shipment.vehicle:
            messages.error(request, 'Assign a driver and vehicle before dispatching.')
            return redirect('logistics_shipment_detail', pk=pk)
        if shipment.status not in ['ready', 'draft']:
            messages.error(request, 'This shipment cannot be dispatched from its current status.')
            return redirect('logistics_shipment_detail', pk=pk)
        shipment.transition(Shipment.Status.DISPATCHED, actor=request.user, note='Dispatched from office')
        try:
            from apps.logistics.notifications import notify_dispatched
            notify_dispatched(shipment)
        except Exception:
            logger.exception('logistics: dispatch notification failed')
        messages.success(request, f'{shipment.reference} dispatched.')
        return redirect('logistics_shipment_detail', pk=pk)


class ShipmentCancelView(_StaffMixin, View):
    http_method_names = ['post']

    def post(self, request, pk):
        shipment = get_object_or_404(Shipment, pk=pk)
        if shipment.is_delivered:
            messages.error(request, 'A delivered shipment cannot be cancelled.')
            return redirect('logistics_shipment_detail', pk=pk)
        shipment.transition(Shipment.Status.CANCELLED, actor=request.user,
                            note=(request.POST.get('reason') or '').strip())
        if shipment.vehicle and not shipment.vehicle.is_hired:
            shipment.vehicle.status = Vehicle.Status.AVAILABLE
            shipment.vehicle.save(update_fields=['status', 'updated_at'])
        messages.success(request, 'Shipment cancelled.')
        return redirect('logistics_shipment_detail', pk=pk)


# ─────────────────────────────────────────────────────────────────────────────
# Live tracking
# ─────────────────────────────────────────────────────────────────────────────

class ShipmentTrackView(_StaffMixin, TemplateView):
    template_name = 'logistics/track.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['shipment'] = get_object_or_404(Shipment, pk=kwargs['pk'])
        return ctx


class ShipmentTrackDataView(_StaffMixin, View):
    def get(self, request, pk):
        shipment = get_object_or_404(Shipment, pk=pk)
        pings = list(
            shipment.location_pings.order_by('recorded_at')
            .values('lat', 'lng', 'recorded_at', 'speed_kph')[:1000]
        )
        for p in pings:
            p['lat'] = float(p['lat'])
            p['lng'] = float(p['lng'])
            p['recorded_at'] = p['recorded_at'].isoformat()
        latest = pings[-1] if pings else None
        return JsonResponse({
            'status': shipment.status,
            'status_display': shipment.get_status_display(),
            'destination': {
                'lat': float(shipment.destination_lat) if shipment.destination_lat is not None else None,
                'lng': float(shipment.destination_lng) if shipment.destination_lng is not None else None,
                'address': shipment.destination_address,
            },
            'pings': pings,
            'latest': latest,
            'delivered': shipment.is_delivered,
        })


# ─────────────────────────────────────────────────────────────────────────────
# Vehicles
# ─────────────────────────────────────────────────────────────────────────────

class VehicleListView(_StaffMixin, TemplateView):
    template_name = 'logistics/vehicle_list.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['vehicles'] = Vehicle.objects.all().order_by('ownership', 'label')
        ctx['ownership_choices'] = Vehicle.Ownership.choices
        ctx['status_choices'] = Vehicle.Status.choices
        return ctx


class VehicleSaveView(_StaffMixin, View):
    http_method_names = ['post']

    def post(self, request):
        pk = request.POST.get('id')
        vehicle = Vehicle.objects.filter(pk=pk).first() if pk else Vehicle()
        vehicle.label = (request.POST.get('label') or '').strip()[:120]
        if not vehicle.label:
            messages.error(request, 'Vehicle label is required.')
            return redirect('logistics_vehicle_list')
        vehicle.registration = (request.POST.get('registration') or '').strip()[:40]
        vehicle.make = (request.POST.get('make') or '').strip()[:80]
        vehicle.model = (request.POST.get('model') or '').strip()[:80]
        vehicle.capacity_note = (request.POST.get('capacity_note') or '').strip()[:120]
        ownership = request.POST.get('ownership')
        if ownership in dict(Vehicle.Ownership.choices):
            vehicle.ownership = ownership
        status = request.POST.get('status')
        if status in dict(Vehicle.Status.choices):
            vehicle.status = status
        vehicle.hire_vendor = (request.POST.get('hire_vendor') or '').strip()[:160]
        vehicle.hire_contact = (request.POST.get('hire_contact') or '').strip()[:80]
        vehicle.hire_rate_note = (request.POST.get('hire_rate_note') or '').strip()[:120]
        vehicle.notes = (request.POST.get('notes') or '').strip()
        vehicle.save()
        messages.success(request, f'Vehicle "{vehicle.label}" saved.')
        return redirect('logistics_vehicle_list')


# ─────────────────────────────────────────────────────────────────────────────
# Drivers + portal links
# ─────────────────────────────────────────────────────────────────────────────

class DriverListView(_StaffMixin, TemplateView):
    template_name = 'logistics/driver_list.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        drivers = Driver.objects.all().order_by('full_name')
        tokens = {t.driver_id: t for t in DriverPortalToken.objects.all()}
        rows = []
        for d in drivers:
            tok = tokens.get(d.id)
            rows.append({
                'driver': d, 'token': tok,
                'has_link': tok is not None,
                'is_active': bool(tok and tok.is_active),
                'link': self.request.build_absolute_uri(f'/drive/{tok.token}/') if tok else '',
                'email_bound': bool(tok and tok.is_email_bound),
                'last_used': tok.last_used_at if tok else None,
            })
        ctx['rows'] = rows
        ctx['staff_users'] = User.objects.filter(is_active=True).order_by('first_name')
        return ctx


class DriverSaveView(_StaffMixin, View):
    http_method_names = ['post']

    def post(self, request):
        pk = request.POST.get('id')
        driver = Driver.objects.filter(pk=pk).first() if pk else Driver()
        driver.full_name = (request.POST.get('full_name') or '').strip()[:160]
        if not driver.full_name:
            messages.error(request, 'Driver name is required.')
            return redirect('logistics_driver_list')
        driver.phone = (request.POST.get('phone') or '').strip()[:40]
        driver.email = (request.POST.get('email') or '').strip()
        driver.license_number = (request.POST.get('license_number') or '').strip()[:80]
        driver.is_external = bool(request.POST.get('is_external'))
        driver.is_active = bool(request.POST.get('is_active', 'on'))
        staff_id = request.POST.get('staff_user')
        driver.staff_user = User.objects.filter(pk=staff_id).first() if staff_id else None
        driver.notes = (request.POST.get('notes') or '').strip()
        driver.save()
        messages.success(request, f'Driver "{driver.full_name}" saved.')
        return redirect('logistics_driver_list')


class DriverLinkActionView(_StaffMixin, View):
    http_method_names = ['post']

    def post(self, request):
        from apps.logistics.models import DriverTrustedDevice, _gen_token
        action = request.POST.get('action', '')
        driver_id = request.POST.get('driver_id')
        token_id = request.POST.get('token_id')

        if action == 'issue':
            driver = Driver.objects.filter(pk=driver_id).first()
            if not driver:
                messages.error(request, 'Driver not found.')
                return redirect('logistics_driver_list')
            tok, created = DriverPortalToken.objects.get_or_create(driver=driver)
            if not created and not tok.is_active:
                tok.is_active = True
                tok.revoked_at = None
                tok.save(update_fields=['is_active', 'revoked_at'])
            messages.success(request, f'Portal link {"created" if created else "active"} for {driver.full_name}.')
            return redirect('logistics_driver_list')

        tok = DriverPortalToken.objects.filter(pk=token_id).first()
        if not tok:
            messages.error(request, 'Link not found.')
            return redirect('logistics_driver_list')
        who = tok.driver.full_name

        if action == 'deactivate':
            tok.revoke()
            messages.success(request, f'{who}\u2019s link deactivated.')
        elif action == 'activate':
            tok.is_active = True
            tok.revoked_at = None
            tok.save(update_fields=['is_active', 'revoked_at'])
            messages.success(request, f'{who}\u2019s link reactivated.')
        elif action == 'regenerate':
            DriverTrustedDevice.objects.filter(token=tok).delete()
            tok.token = _gen_token()
            tok.is_active = True
            tok.revoked_at = None
            tok.save(update_fields=['token', 'is_active', 'revoked_at'])
            messages.success(request, f'{who}\u2019s link regenerated — share the new link.')
        return redirect('logistics_driver_list')


# ─────────────────────────────────────────────────────────────────────────────
# small parse helpers
# ─────────────────────────────────────────────────────────────────────────────

def _dec(raw):
    from decimal import Decimal, InvalidOperation
    try:
        return Decimal(str(raw)) if raw not in (None, '', 'null') else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def _dt(raw):
    if not raw:
        return None
    from django.utils.dateparse import parse_datetime
    val = parse_datetime(raw)
    if val and timezone.is_naive(val):
        val = timezone.make_aware(val)
    return val
