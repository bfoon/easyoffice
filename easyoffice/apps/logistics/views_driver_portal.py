"""
apps/logistics/views_driver_portal.py
======================================

Tokenized, OTP-gated DRIVER portal (no staff login). Same device-trust
pattern as the technician portal.

Routes (see urls_driver_portal.py), all under /drive/<token>/ :
  * landing               — email-bind / device-trust / OTP orchestration
  * deliveries            — list of the driver's open + recent shipments
  * shipment detail       — directions, status buttons, POD capture
  * status update (POST)  — dispatched / en_route / arrived / failed
  * gps ping (POST, JSON) — live location while en route
  * proof of delivery     — capture customer signature → mark delivered
"""
from __future__ import annotations

import json
import logging
import secrets
from datetime import datetime
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.http import JsonResponse, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.generic import View

logger = logging.getLogger(__name__)

DEVICE_COOKIE = 'drive_did'
DEVICE_COOKIE_MAX_AGE = 60 * 60 * 24 * 400
SESSION_OK_PREFIX = 'driver_portal_ok:'


# ─────────────────────────────────────────────────────────────────────────────
# Helpers (mirror the technician portal)
# ─────────────────────────────────────────────────────────────────────────────

def _client_ip(request):
    xff = request.META.get('HTTP_X_FORWARDED_FOR', '')
    return xff.split(',')[0].strip() if xff else request.META.get('REMOTE_ADDR')


def _device_label(request):
    return request.META.get('HTTP_USER_AGENT', '')[:255]


def _ensure_device_cookie(request):
    raw = request.COOKIES.get(DEVICE_COOKIE)
    if raw:
        return raw, False
    return secrets.token_urlsafe(24), True


def _set_device_cookie(response, raw):
    response.set_cookie(
        DEVICE_COOKIE, raw, max_age=DEVICE_COOKIE_MAX_AGE,
        httponly=True, samesite='Lax', secure=True,
    )


def _hash(raw):
    from apps.logistics.models import hash_device_id
    return hash_device_id(raw)


def _mask_email(email):
    if not email or '@' not in email:
        return email or ''
    name, domain = email.split('@', 1)
    masked = (name[0] + '•') if len(name) <= 2 else (name[0] + '•' * (len(name) - 2) + name[-1])
    return f'{masked}@{domain}'


def _resolve_token(token_str):
    from apps.logistics.models import DriverPortalToken
    return DriverPortalToken.objects.filter(token=token_str).select_related('driver').first()


def _skey(tok):
    return f'{SESSION_OK_PREFIX}{tok.id}'


def _session_ok(request, tok):
    return bool(request.session.get(_skey(tok)))


def _mark_ok(request, tok):
    request.session[_skey(tok)] = True
    request.session.set_expiry(0)


def _send_driver_otp(otp, *, token):
    from django.conf import settings
    from django.core.mail import EmailMultiAlternatives
    from django.template.loader import render_to_string
    ctx = {
        'code': otp.code, 'expires_minutes': 10,
        'contact': token.driver, 'contract': None,
        'device_label': otp.device_label or 'a new device',
        'requested_ip': otp.requested_ip or '—',
        'now': timezone.now(), 'is_rebind': True,
    }
    try:
        html = render_to_string('logistics/driver/otp_email.html', ctx)
    except Exception:
        html = f'Your delivery portal code is {otp.code} (expires in 10 minutes).'
    msg = EmailMultiAlternatives(
        subject='Your delivery portal verification code',
        body=f'Your code is {otp.code}. It expires in 10 minutes.',
        from_email=getattr(settings, 'DEFAULT_FROM_EMAIL', None),
        to=[otp.sent_to],
    )
    msg.attach_alternative(html, 'text/html')
    msg.send(fail_silently=False)


def _to_decimal(raw):
    try:
        return Decimal(str(raw)) if raw not in (None, '', 'null') else None
    except (InvalidOperation, ValueError, TypeError):
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Landing — email bind / device trust / OTP
# ─────────────────────────────────────────────────────────────────────────────

class DriverPortalLandingView(View):
    def get(self, request, token):
        tok = _resolve_token(token)
        if not tok or not tok.is_active:
            reason = 'revoked' if tok and not tok.is_active else 'notfound'
            return render(request, 'logistics/driver/invalid_token.html', {'reason': reason})

        raw, set_cookie = _ensure_device_cookie(request)
        dh = _hash(raw)

        if not tok.is_email_bound:
            resp = render(request, 'logistics/driver/bind_email.html', {'tok': tok})
            if set_cookie:
                _set_device_cookie(resp, raw)
            return resp

        if _session_ok(request, tok):
            return redirect('driver_deliveries', token=tok.token)

        from apps.logistics.models import DriverTrustedDevice
        if DriverTrustedDevice.is_trusted(token=tok, device_hash=dh):
            _mark_ok(request, tok)
            tok.touch()
            resp = redirect('driver_deliveries', token=tok.token)
            if set_cookie:
                _set_device_cookie(resp, raw)
            return resp

        return self._issue_and_show(request, tok, dh, raw, set_cookie, is_rebind=True)

    def post(self, request, token):
        tok = _resolve_token(token)
        if not tok or not tok.is_active:
            return render(request, 'logistics/driver/invalid_token.html', {'reason': 'notfound'})

        raw, set_cookie = _ensure_device_cookie(request)
        dh = _hash(raw)
        action = request.POST.get('action', '')

        # Email binding (first open)
        if not tok.is_email_bound:
            email = (request.POST.get('email') or '').strip().lower()
            if not email or '@' not in email:
                messages.error(request, 'Please enter a valid email address.')
                resp = render(request, 'logistics/driver/bind_email.html', {'tok': tok})
                if set_cookie:
                    _set_device_cookie(resp, raw)
                return resp
            acct = (tok.driver.email or '').strip().lower()
            if acct and email != acct:
                messages.error(request, 'That email does not match the address on file for this link.')
                resp = render(request, 'logistics/driver/bind_email.html', {'tok': tok})
                if set_cookie:
                    _set_device_cookie(resp, raw)
                return resp
            tok.bind_email(email)
            return self._issue_and_show(request, tok, dh, raw, set_cookie, is_rebind=False)

        if action == 'resend':
            return self._issue_and_show(request, tok, dh, raw, set_cookie, is_rebind=True,
                                        flash='A new code is on its way.')

        # verify
        from apps.logistics.models import DriverOTP, DriverTrustedDevice
        ok, reason = DriverOTP.verify(token=tok, device_hash=dh, code=(request.POST.get('code') or ''))
        if not ok:
            resp = render(request, 'logistics/driver/verify_device.html', {
                'tok': tok, 'masked_email': _mask_email(tok.resolved_email),
                'expires_minutes': 10, 'device_label': _device_label(request),
                'client_ip': _client_ip(request), 'is_rebind': True, 'error': reason,
            })
            if set_cookie:
                _set_device_cookie(resp, raw)
            return resp

        DriverTrustedDevice.grant(token=tok, device_hash=dh,
                                  device_label=_device_label(request), ip=_client_ip(request))
        _mark_ok(request, tok)
        tok.touch()
        resp = redirect('driver_deliveries', token=tok.token)
        if set_cookie:
            _set_device_cookie(resp, raw)
        return resp

    def _issue_and_show(self, request, tok, dh, raw, set_cookie, *, is_rebind, flash=''):
        from apps.logistics.models import DriverOTP
        otp = DriverOTP.issue(token=tok, device_hash=dh, sent_to=tok.resolved_email,
                              ip=_client_ip(request), device_label=_device_label(request))
        try:
            _send_driver_otp(otp, token=tok)
            if flash:
                messages.success(request, flash)
        except Exception:
            logger.exception('driver portal: OTP send failed')
            messages.error(request, 'We could not send the code. Please try again shortly.')
        resp = render(request, 'logistics/driver/verify_device.html', {
            'tok': tok, 'masked_email': _mask_email(tok.resolved_email),
            'expires_minutes': 10, 'device_label': _device_label(request),
            'client_ip': _client_ip(request), 'is_rebind': is_rebind,
        })
        if set_cookie:
            _set_device_cookie(resp, raw)
        return resp


# ─────────────────────────────────────────────────────────────────────────────
# Gate mixin
# ─────────────────────────────────────────────────────────────────────────────

class _DriverGate:
    def _gate(self, request, token):
        tok = _resolve_token(token)
        if not tok or not tok.is_active:
            return None, render(request, 'logistics/driver/invalid_token.html', {'reason': 'notfound'})
        if not _session_ok(request, tok):
            return None, redirect('driver_portal_landing', token=tok.token)
        return tok, None


# ─────────────────────────────────────────────────────────────────────────────
# Deliveries list + detail
# ─────────────────────────────────────────────────────────────────────────────

class DriverDeliveriesView(_DriverGate, View):
    def get(self, request, token):
        tok, bounce = self._gate(request, token)
        if bounce:
            return bounce
        from apps.logistics.models import Shipment
        qs = Shipment.objects.filter(driver=tok.driver).select_related('vehicle').prefetch_related('items')
        open_shipments = qs.filter(status__in=Shipment.OPEN_STATUSES).order_by('scheduled_for', '-created_at')
        recent = qs.filter(status__in=['delivered', 'failed', 'cancelled']).order_by('-updated_at')[:10]
        return render(request, 'logistics/driver/deliveries.html', {
            'tok': tok, 'driver': tok.driver,
            'open_shipments': open_shipments, 'recent': recent,
        })


class DriverShipmentDetailView(_DriverGate, View):
    def get(self, request, token, pk):
        tok, bounce = self._gate(request, token)
        if bounce:
            return bounce
        shipment, deny = self._owned(tok, pk)
        if deny:
            return deny
        from apps.logistics.models import Shipment
        return render(request, 'logistics/driver/delivery_detail.html', {
            'tok': tok, 'shipment': shipment,
            'items': shipment.items.all(),
            'events': shipment.events.all(),
            'can_act': shipment.is_open,
            'status_flow': Shipment.Status,
        })

    def _owned(self, tok, pk):
        from apps.logistics.models import Shipment
        s = Shipment.objects.filter(pk=pk).select_related('vehicle').first()
        if not s:
            return None, redirect('driver_deliveries', token=tok.token)
        if s.driver_id != tok.driver_id:
            return None, HttpResponseForbidden('This delivery is assigned to another driver.')
        return s, None


# ─────────────────────────────────────────────────────────────────────────────
# Status updates
# ─────────────────────────────────────────────────────────────────────────────

class DriverStatusUpdateView(_DriverGate, View):
    http_method_names = ['post']

    ALLOWED = {'dispatched', 'en_route', 'arrived', 'failed'}

    def post(self, request, token, pk):
        tok, bounce = self._gate(request, token)
        if bounce:
            return bounce
        from apps.logistics.models import Shipment
        shipment = get_object_or_404(Shipment, pk=pk)
        if shipment.driver_id != tok.driver_id:
            return HttpResponseForbidden('Not your delivery.')

        new_status = request.POST.get('status', '')
        if new_status not in self.ALLOWED:
            messages.error(request, 'Invalid status update.')
            return redirect('driver_shipment_detail', token=tok.token, pk=pk)
        if not shipment.is_open:
            messages.error(request, 'This delivery is already closed.')
            return redirect('driver_shipment_detail', token=tok.token, pk=pk)

        note = (request.POST.get('note') or '').strip()
        shipment.transition(new_status, note=note, by_driver=True)
        messages.success(request, f'Updated to "{shipment.get_status_display()}".')
        return redirect('driver_shipment_detail', token=tok.token, pk=pk)


# ─────────────────────────────────────────────────────────────────────────────
# Live GPS ping (JSON, called from the detail page via navigator.geolocation)
# ─────────────────────────────────────────────────────────────────────────────

class DriverGpsPingView(_DriverGate, View):
    http_method_names = ['post']

    def post(self, request, token, pk):
        tok, bounce = self._gate(request, token)
        if bounce:
            # bounce is a redirect/HttpResponse; for JSON callers return 401-ish
            return JsonResponse({'ok': False, 'error': 'auth'}, status=403)

        from apps.logistics.models import Shipment, LocationPing
        shipment = Shipment.objects.filter(pk=pk).first()
        if not shipment or shipment.driver_id != tok.driver_id:
            return JsonResponse({'ok': False, 'error': 'not_found'}, status=404)
        if not shipment.is_open:
            return JsonResponse({'ok': False, 'error': 'closed'}, status=409)

        try:
            payload = json.loads(request.body or '{}')
        except json.JSONDecodeError:
            payload = request.POST

        lat = _to_decimal(payload.get('lat'))
        lng = _to_decimal(payload.get('lng'))
        if lat is None or lng is None:
            return JsonResponse({'ok': False, 'error': 'bad_coords'}, status=400)

        def _f(v):
            try:
                return float(v) if v not in (None, '', 'null') else None
            except (ValueError, TypeError):
                return None

        LocationPing.objects.create(
            shipment=shipment, lat=lat, lng=lng,
            accuracy_m=_f(payload.get('accuracy')),
            speed_kph=_f(payload.get('speed')),
            heading=_f(payload.get('heading')),
        )
        # First ping after dispatch flips to en_route automatically.
        if shipment.status == Shipment.Status.DISPATCHED:
            shipment.transition(Shipment.Status.EN_ROUTE, by_driver=True,
                                note='Auto: movement detected')
        return JsonResponse({'ok': True})


# ─────────────────────────────────────────────────────────────────────────────
# Proof of delivery (customer sign-off)
# ─────────────────────────────────────────────────────────────────────────────

class DriverProofOfDeliveryView(_DriverGate, View):
    def get(self, request, token, pk):
        tok, bounce = self._gate(request, token)
        if bounce:
            return bounce
        from apps.logistics.models import Shipment
        shipment = get_object_or_404(Shipment, pk=pk)
        if shipment.driver_id != tok.driver_id:
            return HttpResponseForbidden('Not your delivery.')
        return render(request, 'logistics/driver/proof_of_delivery.html', {
            'tok': tok, 'shipment': shipment,
        })

    def post(self, request, token, pk):
        tok, bounce = self._gate(request, token)
        if bounce:
            return bounce
        from apps.logistics.models import Shipment, ProofOfDelivery
        shipment = get_object_or_404(Shipment, pk=pk)
        if shipment.driver_id != tok.driver_id:
            return HttpResponseForbidden('Not your delivery.')
        if shipment.is_delivered:
            messages.info(request, 'This delivery is already signed off.')
            return redirect('driver_shipment_detail', token=tok.token, pk=pk)

        received_by = (request.POST.get('received_by_name') or '').strip()
        sig_data = (request.POST.get('signature_data_url') or '').strip()
        sig_typed = (request.POST.get('signature_typed') or '').strip()
        if not received_by:
            messages.error(request, 'Please enter who received the delivery.')
            return redirect('driver_proof_of_delivery', token=tok.token, pk=pk)
        if not sig_data and not sig_typed:
            messages.error(request, 'A signature is required to complete delivery.')
            return redirect('driver_proof_of_delivery', token=tok.token, pk=pk)

        pod, _ = ProofOfDelivery.objects.get_or_create(shipment=shipment)
        pod.received_by_name = received_by[:160]
        pod.signature_data_url = sig_data if sig_data.startswith('data:image') else ''
        pod.signature_typed = sig_typed[:160]
        pod.note = (request.POST.get('note') or '').strip()
        pod.signed_lat = _to_decimal(request.POST.get('lat'))
        pod.signed_lng = _to_decimal(request.POST.get('lng'))
        pod.signed_ip = _client_ip(request)
        pod.signed_at = timezone.now()
        if request.FILES.get('photo'):
            pod.photo = request.FILES['photo']
        pod.save()

        shipment.transition(Shipment.Status.DELIVERED, by_driver=True,
                            note=f'Signed by {received_by}')
        # Best-effort customer notification.
        try:
            from apps.logistics.notifications import notify_delivered
            notify_delivered(shipment)
        except Exception:
            logger.exception('logistics: delivered notification failed')

        messages.success(request, 'Delivery completed and signed. Thank you!')
        return redirect('driver_shipment_detail', token=tok.token, pk=pk)
