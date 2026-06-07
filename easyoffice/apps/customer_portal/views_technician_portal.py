"""
apps/customer_portal/views_technician_portal.py
================================================

NEW MODULE — tokenized, OTP-gated TECHNICIAN portal (no staff login).

Flow on opening /tech/<token>/ :
  1. Resolve the token. Inactive/unknown → invalid-token page.
  2. First open ever → ask for email once (binds the token).
  3. Device check (cookie hashed):
       * trusted & unexpired  → straight in (trust auto-renews 30d).
       * not trusted / expired → issue OTP, show verify page.
  4. Verified / trusted → entry list. Create new entries; edit/delete an
     entry only while its month has NOT yet been rolled into a signed
     monthly report.

Session model: once a device passes (trusted or OTP-verified) we set a
signed session flag keyed to the token so the technician doesn't re-verify
on every request within the browser session; the 30-day device trust is
the durable layer across sessions.
"""
from __future__ import annotations

import logging
from datetime import date

from django.contrib import messages
from django.http import HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.generic import View

logger = logging.getLogger(__name__)

DEVICE_COOKIE = 'tech_did'           # raw device id cookie (random, per browser)
DEVICE_COOKIE_MAX_AGE = 60 * 60 * 24 * 400   # ~13 months
SESSION_OK_PREFIX = 'tech_portal_ok:'        # + token id → bool


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _client_ip(request):
    xff = request.META.get('HTTP_X_FORWARDED_FOR', '')
    if xff:
        return xff.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR')


def _device_label(request):
    ua = request.META.get('HTTP_USER_AGENT', '')
    return ua[:255]


def _ensure_device_cookie(request):
    """Return (raw_device_id, set_cookie?) — generates one if missing."""
    raw = request.COOKIES.get(DEVICE_COOKIE)
    if raw:
        return raw, False
    import secrets
    return secrets.token_urlsafe(24), True


def _mask_email(email: str) -> str:
    if not email or '@' not in email:
        return email or ''
    name, domain = email.split('@', 1)
    if len(name) <= 2:
        masked = name[0] + '•'
    else:
        masked = name[0] + '•' * (len(name) - 2) + name[-1]
    return f'{masked}@{domain}'


def _resolve_token(token_str):
    from apps.customer_portal.models_technician_portal import TechnicianPortalToken
    return TechnicianPortalToken.objects.filter(token=token_str).select_related('technician').first()


def _session_key(tok):
    return f'{SESSION_OK_PREFIX}{tok.id}'


def _is_session_ok(request, tok):
    return bool(request.session.get(_session_key(tok)))


def _mark_session_ok(request, tok):
    request.session[_session_key(tok)] = True
    request.session.set_expiry(0)  # browser-session; device-trust is the durable layer


def _send_tech_otp(otp, *, token, request):
    """Email the OTP using the existing device_otp.html email template."""
    from django.conf import settings
    from django.core.mail import EmailMultiAlternatives
    from django.template.loader import render_to_string

    ctx = {
        'code': otp.code,
        'expires_minutes': 10,
        'contact': token.technician,
        'contract': None,
        'device_label': otp.device_label or 'a new device',
        'requested_ip': otp.requested_ip or '—',
        'now': timezone.now(),
        'is_rebind': True,
    }
    try:
        html = render_to_string('customer_portal/device_otp.html', ctx)
    except Exception:
        html = f'Your verification code is {otp.code} (expires in 10 minutes).'

    subject = 'Your technician portal verification code'
    body = f'Your verification code is {otp.code}. It expires in 10 minutes.'
    msg = EmailMultiAlternatives(
        subject=subject, body=body,
        from_email=getattr(settings, 'DEFAULT_FROM_EMAIL', None),
        to=[otp.sent_to],
    )
    msg.attach_alternative(html, 'text/html')
    msg.send(fail_silently=False)


def _entry_is_locked(visit) -> bool:
    """
    An entry (visit) is editable until its month has been rolled into a
    monthly report. Locked if:
      * a MonthlyServiceReport covering the visit's month + contract exists
        in a status beyond draft (awaiting_sign / signed / sent), OR
      * the visit itself is already marked REPORTED.
    """
    from apps.customer_portal.models_onsite_report import (
        OnSiteVisitReport, MonthlyServiceReport,
    )
    if visit.status == OnSiteVisitReport.Status.REPORTED:
        return True

    contract = visit.resolved_contract
    vd = visit.visit_date
    qs = MonthlyServiceReport.objects.filter(
        period_start__lte=vd,
        period_end__gte=vd,
        status__in=[
            MonthlyServiceReport.Status.AWAITING_SIGN,
            MonthlyServiceReport.Status.SIGNED,
            MonthlyServiceReport.Status.SENT,
        ],
    )
    if contract is not None:
        qs = qs.filter(contract=contract)
    else:
        # customer-scope reports — match on customer name if we can
        cust = getattr(visit.resolved_contract, 'counterparty_name', '') if visit.resolved_contract else ''
        if cust:
            qs = qs.filter(customer_name__iexact=cust)
        else:
            qs = qs.none()
    return qs.exists()


# ─────────────────────────────────────────────────────────────────────────────
# Token landing — orchestrates email bind / device trust / OTP
# ─────────────────────────────────────────────────────────────────────────────

class TechnicianPortalLandingView(View):
    """GET/POST /tech/<token>/"""

    def get(self, request, token):
        tok = _resolve_token(token)
        if not tok or not tok.is_active:
            reason = 'revoked' if tok and not tok.is_active else 'notfound'
            return render(request, 'customer_portal/technician/invalid_token.html',
                          {'reason': reason})

        raw_device, set_cookie = _ensure_device_cookie(request)
        device_hash = _hash(raw_device)

        # 1. Email not yet bound → capture it once.
        if not tok.is_email_bound:
            resp = render(request, 'customer_portal/technician/bind_email.html', {'tok': tok})
            if set_cookie:
                _set_device_cookie(resp, raw_device)
            return resp

        # 2. Already OK this session → straight to entries.
        if _is_session_ok(request, tok):
            return redirect('technician_portal_entries', token=tok.token)

        # 3. Device trust check.
        from apps.customer_portal.models_technician_portal import TechnicianTrustedDevice
        if TechnicianTrustedDevice.is_trusted(token=tok, device_hash=device_hash):
            _mark_session_ok(request, tok)
            tok.touch()
            resp = redirect('technician_portal_entries', token=tok.token)
            if set_cookie:
                _set_device_cookie(resp, raw_device)
            return resp

        # 4. Untrusted / expired → issue OTP + show verify page.
        from apps.customer_portal.models_technician_portal import TechnicianOTP
        otp = TechnicianOTP.issue(
            token=tok, device_hash=device_hash,
            sent_to=tok.resolved_email, ip=_client_ip(request),
            device_label=_device_label(request),
        )
        try:
            _send_tech_otp(otp, token=tok, request=request)
        except Exception:
            logger.exception('technician portal: OTP send failed')
            messages.error(request, 'We could not send the code. Please try again shortly.')

        resp = render(request, 'customer_portal/technician/verify_device.html', {
            'tok': tok,
            'masked_email': _mask_email(tok.resolved_email),
            'expires_minutes': 10,
            'device_label': _device_label(request),
            'client_ip': _client_ip(request),
            'is_rebind': True,
        })
        if set_cookie:
            _set_device_cookie(resp, raw_device)
        return resp

    def post(self, request, token):
        """Handles: email binding, OTP verify, OTP resend."""
        tok = _resolve_token(token)
        if not tok or not tok.is_active:
            return render(request, 'customer_portal/technician/invalid_token.html',
                          {'reason': 'notfound'})

        raw_device, set_cookie = _ensure_device_cookie(request)
        device_hash = _hash(raw_device)
        action = request.POST.get('action', '')

        # ── Email binding (first open) ───────────────────────────────────
        if not tok.is_email_bound:
            email = (request.POST.get('email') or '').strip().lower()
            if not email or '@' not in email:
                messages.error(request, 'Please enter a valid email address.')
                resp = render(request, 'customer_portal/technician/bind_email.html', {'tok': tok})
                if set_cookie:
                    _set_device_cookie(resp, raw_device)
                return resp

            # Optional: enforce match against the staff account email.
            acct_email = (tok.technician.email or '').strip().lower()
            if acct_email and email != acct_email:
                messages.error(
                    request,
                    'That email does not match the address on file for this link.',
                )
                resp = render(request, 'customer_portal/technician/bind_email.html', {'tok': tok})
                if set_cookie:
                    _set_device_cookie(resp, raw_device)
                return resp

            tok.bind_email(email)
            # Now go through the device/OTP flow with a fresh GET.
            from apps.customer_portal.models_technician_portal import TechnicianOTP
            otp = TechnicianOTP.issue(
                token=tok, device_hash=device_hash,
                sent_to=tok.resolved_email, ip=_client_ip(request),
                device_label=_device_label(request),
            )
            try:
                _send_tech_otp(otp, token=tok, request=request)
            except Exception:
                logger.exception('technician portal: first OTP send failed')
            resp = render(request, 'customer_portal/technician/verify_device.html', {
                'tok': tok,
                'masked_email': _mask_email(tok.resolved_email),
                'expires_minutes': 10,
                'device_label': _device_label(request),
                'client_ip': _client_ip(request),
                'is_rebind': False,
            })
            if set_cookie:
                _set_device_cookie(resp, raw_device)
            return resp

        # ── OTP resend ────────────────────────────────────────────────────
        if action == 'resend':
            from apps.customer_portal.models_technician_portal import TechnicianOTP
            otp = TechnicianOTP.issue(
                token=tok, device_hash=device_hash,
                sent_to=tok.resolved_email, ip=_client_ip(request),
                device_label=_device_label(request),
            )
            try:
                _send_tech_otp(otp, token=tok, request=request)
                messages.success(request, 'A new code is on its way.')
            except Exception:
                logger.exception('technician portal: OTP resend failed')
                messages.error(request, 'Could not resend the code.')
            return render(request, 'customer_portal/technician/verify_device.html', {
                'tok': tok,
                'masked_email': _mask_email(tok.resolved_email),
                'expires_minutes': 10,
                'device_label': _device_label(request),
                'client_ip': _client_ip(request),
                'is_rebind': True,
            })

        # ── OTP verify ────────────────────────────────────────────────────
        from apps.customer_portal.models_technician_portal import (
            TechnicianOTP, TechnicianTrustedDevice,
        )
        code = (request.POST.get('code') or '').strip()
        ok, reason = TechnicianOTP.verify(token=tok, device_hash=device_hash, code=code)
        if not ok:
            resp = render(request, 'customer_portal/technician/verify_device.html', {
                'tok': tok,
                'masked_email': _mask_email(tok.resolved_email),
                'expires_minutes': 10,
                'device_label': _device_label(request),
                'client_ip': _client_ip(request),
                'is_rebind': True,
                'error': reason,
            })
            if set_cookie:
                _set_device_cookie(resp, raw_device)
            return resp

        # Success → trust the device 30 days + mark session.
        TechnicianTrustedDevice.grant(
            token=tok, device_hash=device_hash,
            device_label=_device_label(request), ip=_client_ip(request),
        )
        _mark_session_ok(request, tok)
        tok.touch()
        resp = redirect('technician_portal_entries', token=tok.token)
        if set_cookie:
            _set_device_cookie(resp, raw_device)
        return resp


# ─────────────────────────────────────────────────────────────────────────────
# Gate mixin for the entry pages
# ─────────────────────────────────────────────────────────────────────────────

class _TechGateMixin:
    """Resolve token + require a passed session, else bounce to landing."""

    def _gate(self, request, token):
        tok = _resolve_token(token)
        if not tok or not tok.is_active:
            return None, render(
                request, 'customer_portal/technician/invalid_token.html',
                {'reason': 'notfound'},
            )
        if not _is_session_ok(request, tok):
            # Re-run the device/OTP flow.
            return None, redirect('technician_portal_landing', token=tok.token)
        return tok, None


# ─────────────────────────────────────────────────────────────────────────────
# Entry list
# ─────────────────────────────────────────────────────────────────────────────

class TechnicianEntryListView(_TechGateMixin, View):
    def get(self, request, token):
        tok, bounce = self._gate(request, token)
        if bounce:
            return bounce

        from apps.customer_portal.models_onsite_report import OnSiteVisitReport
        visits = (
            OnSiteVisitReport.objects
            .filter(technician=tok.technician)
            .select_related('contract')
            .prefetch_related('activities')
            .order_by('-visit_date', '-created_at')
        )
        rows = [{'visit': v, 'locked': _entry_is_locked(v)} for v in visits]
        return render(request, 'customer_portal/technician/entry_list.html', {
            'tok': tok, 'rows': rows, 'technician': tok.technician,
        })


# ─────────────────────────────────────────────────────────────────────────────
# Create / edit entry
# ─────────────────────────────────────────────────────────────────────────────

class TechnicianEntryCreateView(_TechGateMixin, View):
    def get(self, request, token):
        tok, bounce = self._gate(request, token)
        if bounce:
            return bounce
        return render(request, 'customer_portal/technician/entry_form.html', {
            'tok': tok,
            'contracts': self._tech_contracts(tok),
            'origin_choices': self._origins(),
            'status_choices': self._statuses(),
            'today': timezone.localdate(),
            'mode': 'create',
        })

    def post(self, request, token):
        tok, bounce = self._gate(request, token)
        if bounce:
            return bounce

        from apps.customer_portal.models_onsite_report import OnSiteVisitReport
        from apps.finance.models import Contract

        origin = request.POST.get('origin') or OnSiteVisitReport.Origin.ROUTINE
        if origin not in dict(self._origins()):
            origin = OnSiteVisitReport.Origin.ROUTINE

        visit_date = _parse_date(request.POST.get('visit_date')) or timezone.localdate()
        contract = None
        cid = request.POST.get('contract')
        if cid:
            contract = Contract.objects.filter(pk=cid).first()

        visit = OnSiteVisitReport.objects.create(
            origin=origin,
            contract=contract,
            technician=tok.technician,
            visit_date=visit_date,
            site_location=(request.POST.get('site_location') or '').strip()[:255],
            summary=(request.POST.get('summary') or '').strip(),
        )
        messages.success(request, 'Entry created. Add your activity rows below.')
        return redirect('technician_entry_edit', token=tok.token, pk=visit.pk)

    # helpers
    def _tech_contracts(self, tok):
        from apps.finance.models import Contract
        return Contract.objects.filter(status='active').order_by('title')

    def _origins(self):
        from apps.customer_portal.models_onsite_report import OnSiteVisitReport
        return OnSiteVisitReport.Origin.choices

    def _statuses(self):
        from apps.customer_portal.models_onsite_report import VisitActivity
        return VisitActivity.Status.choices


class TechnicianEntryEditView(_TechGateMixin, View):
    def get(self, request, token, pk):
        tok, bounce = self._gate(request, token)
        if bounce:
            return bounce
        visit, deny = self._owned_visit(request, tok, pk)
        if deny:
            return deny

        from apps.customer_portal.models_onsite_report import VisitActivity
        return render(request, 'customer_portal/technician/entry_form.html', {
            'tok': tok,
            'visit': visit,
            'activities': visit.activities.all(),
            'locked': _entry_is_locked(visit),
            'status_choices': VisitActivity.Status.choices,
            'today': timezone.localdate(),
            'mode': 'edit',
        })

    def post(self, request, token, pk):
        tok, bounce = self._gate(request, token)
        if bounce:
            return bounce
        visit, deny = self._owned_visit(request, tok, pk)
        if deny:
            return deny

        if _entry_is_locked(visit):
            messages.error(request, 'This entry\'s month has been reported and can no longer be changed.')
            return redirect('technician_entry_edit', token=tok.token, pk=visit.pk)

        action = request.POST.get('action', '')
        from apps.customer_portal.models_onsite_report import VisitActivity

        if action == 'add_activity':
            desc = (request.POST.get('task_description') or '').strip()
            if not desc:
                messages.error(request, 'Issue / task is required.')
                return redirect('technician_entry_edit', token=tok.token, pk=visit.pk)
            status = request.POST.get('status') or VisitActivity.Status.DONE
            if status not in dict(VisitActivity.Status.choices):
                status = VisitActivity.Status.DONE
            VisitActivity.objects.create(
                visit=visit,
                position=visit.activities.count(),
                activity_date=_parse_date(request.POST.get('activity_date')) or visit.visit_date,
                device=(request.POST.get('device') or '').strip()[:200],
                task_description=desc[:500],
                serial_or_user=(request.POST.get('serial_or_user') or '').strip()[:200],
                status=status,
                notes=(request.POST.get('notes') or '').strip(),
                logged_by=tok.technician,
            )
            messages.success(request, 'Activity added.')
            return redirect('technician_entry_edit', token=tok.token, pk=visit.pk)

        if action == 'delete_activity':
            act = visit.activities.filter(pk=request.POST.get('activity_id')).first()
            if act:
                act.delete()
                messages.success(request, 'Activity removed.')
            return redirect('technician_entry_edit', token=tok.token, pk=visit.pk)

        # default: save header fields
        visit.visit_date = _parse_date(request.POST.get('visit_date')) or visit.visit_date
        visit.site_location = (request.POST.get('site_location') or '').strip()[:255]
        visit.summary = (request.POST.get('summary') or '').strip()
        visit.save(update_fields=['visit_date', 'site_location', 'summary', 'updated_at'])
        messages.success(request, 'Entry updated.')
        return redirect('technician_entry_edit', token=tok.token, pk=visit.pk)

    def _owned_visit(self, request, tok, pk):
        from apps.customer_portal.models_onsite_report import OnSiteVisitReport
        visit = OnSiteVisitReport.objects.filter(pk=pk).select_related('contract').first()
        if not visit:
            return None, redirect('technician_portal_entries', token=tok.token)
        if visit.technician_id != tok.technician_id:
            return None, HttpResponseForbidden('This entry belongs to another technician.')
        return visit, None


class TechnicianEntryDeleteView(_TechGateMixin, View):
    def post(self, request, token, pk):
        tok, bounce = self._gate(request, token)
        if bounce:
            return bounce
        from apps.customer_portal.models_onsite_report import OnSiteVisitReport
        visit = OnSiteVisitReport.objects.filter(pk=pk).first()
        if not visit or visit.technician_id != tok.technician_id:
            return redirect('technician_portal_entries', token=tok.token)
        if _entry_is_locked(visit):
            messages.error(request, 'This entry\'s month has been reported and can no longer be deleted.')
            return redirect('technician_portal_entries', token=tok.token)
        visit.delete()
        messages.success(request, 'Entry deleted.')
        return redirect('technician_portal_entries', token=tok.token)


# ─────────────────────────────────────────────────────────────────────────────
# Low-level helpers
# ─────────────────────────────────────────────────────────────────────────────

def _hash(raw):
    from apps.customer_portal.models_technician_portal import hash_device_id
    return hash_device_id(raw)


def _set_device_cookie(response, raw):
    response.set_cookie(
        DEVICE_COOKIE, raw,
        max_age=DEVICE_COOKIE_MAX_AGE,
        httponly=True, samesite='Lax', secure=True,
    )


def _parse_date(raw):
    from datetime import datetime
    if not raw:
        return None
    for fmt in ('%Y-%m-%d', '%d/%m/%Y', '%d-%m-%Y'):
        try:
            return datetime.strptime(raw.strip(), fmt).date()
        except ValueError:
            continue
    return None
