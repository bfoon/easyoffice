import math
import secrets
import logging
from datetime import timedelta

import requests as http_requests
from django.contrib.auth import login, logout, update_session_auth_hash
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.forms import AuthenticationForm, PasswordChangeForm
from django.views.generic import View, TemplateView
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.http import JsonResponse, HttpResponseForbidden
from django.utils import timezone
from django.core.mail import send_mail
from django.conf import settings
from django.urls import reverse

from apps.core.models import (
    User, CoreNotification,
    TrustedDevice, OTPChallenge, DeviceSwitchToken,
    SecuritySettings, SecurityEvent,
)

logger = logging.getLogger(__name__)

DEVICE_COOKIE  = '_did'
SESSION_OTP_ID = '_otp_id'
SESSION_SWITCH = '_switch_id'
COOKIE_MAX_AGE = 60 * 60 * 24 * 400   # ~13 months

# OTP bypass: a superuser may temporarily disable OTP on a user's account.
# These constants bound the maximum bypass window — the view rejects any
# request that exceeds this. See ToggleOTPView.
OTP_BYPASS_MAX_HOURS = 168    # 7 days
OTP_BYPASS_MIN_HOURS = 1


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _get_client_ip(request):
    xff = request.META.get('HTTP_X_FORWARDED_FOR', '')
    return xff.split(',')[0].strip() if xff else request.META.get('REMOTE_ADDR', '')


def _get_device_id(request):
    return request.COOKIES.get(DEVICE_COOKIE, '').strip()


def _new_device_id():
    return secrets.token_hex(32)


def _set_device_cookie(response, device_id):
    response.set_cookie(
        DEVICE_COOKIE, device_id,
        max_age=COOKIE_MAX_AGE,
        httponly=True,
        samesite='Lax',
        secure=not settings.DEBUG,
    )


def _ua_summary(request):
    ua = request.META.get('HTTP_USER_AGENT', '')
    browser = next((n for n, k in [
        ('Chrome', 'Chrome/'),
        ('Firefox', 'Firefox/'),
        ('Safari', 'Safari/'),
        ('Edge', 'Edg/'),
        ('Opera', 'OPR/')
    ] if k in ua), 'Browser')

    os_name = next((n for n, k in [
        ('Windows', 'Windows NT'),
        ('macOS', 'Mac OS X'),
        ('Android', 'Android'),
        ('iOS', 'iPhone'),
        ('Linux', 'Linux')
    ] if k in ua), 'Unknown OS')

    return f'{browser} on {os_name}'


def _geo_lookup(ip):
    if not ip or ip.startswith(('10.', '127.', '192.168.', '172.', '::1')):
        return {}
    try:
        r = http_requests.get(
            f'http://ip-api.com/json/{ip}?fields=status,country,city,lat,lon',
            timeout=4,
        )
        if r.status_code == 200:
            j = r.json()
            if j.get('status') == 'success':
                return {
                    'lat': float(j.get('lat', 0)),
                    'lng': float(j.get('lon', 0)),
                    'city': j.get('city', ''),
                    'country': j.get('country', '')
                }
    except Exception:
        pass
    return {}


def _haversine_km(lat1, lng1, lat2, lng2):
    R = 6371
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
        * math.sin(dlng / 2) ** 2
    )
    return R * 2 * math.asin(math.sqrt(a))


def _notify_admins(title, message, link=''):
    for admin in User.objects.filter(is_active=True, is_staff=True):
        CoreNotification.objects.create(
            recipient=admin,
            notification_type='security',
            title=title,
            message=message,
            link=link,
        )


def _notify_user(user, title, message, link=''):
    CoreNotification.objects.create(
        recipient=user,
        notification_type='security',
        title=title,
        message=message,
        link=link,
    )


def _send_otp_email(user, otp):
    if not user.email:
        logger.warning("OTP email not sent: user %s has no email address", user.pk)
        return False

    try:
        sent = send_mail(
            subject='Your EasyOffice login verification code',
            message=(
                f'Hi {user.first_name or user.email},\n\n'
                f'Your one-time verification code is:\n\n'
                f'    {otp.code}\n\n'
                f'This code expires in {OTPChallenge.EXPIRY_MINUTES} minutes.\n'
                f'Do not share it with anyone.\n\n'
                f'If you did not attempt to log in, contact your administrator immediately.\n'
            ),
            from_email=getattr(settings, 'DEFAULT_FROM_EMAIL', None),
            recipient_list=[user.email],
            fail_silently=False,
        )
        logger.info("OTP email sent to %s (sent=%s)", user.email, sent)
        return sent > 0
    except Exception:
        logger.exception("OTP email failed for %s", user.email)
        return False


def _send_switch_email(user, sw, request):
    url = request.build_absolute_uri(
        reverse('device_switch_approve', kwargs={'token': sw.token})
    )
    loc = f'{sw.new_city}, {sw.new_country}'.strip(', ') or 'Unknown location'
    try:
        send_mail(
            subject='EasyOffice — Approve login from new device',
            message=(
                f'Hi {user.first_name or user.email},\n\n'
                f'A request was made to log in to your account from:\n\n'
                f'    Device  : {sw.new_device_name}\n'
                f'    Location: {loc}\n'
                f'    IP      : {sw.new_ip}\n\n'
                f'If this is you, click below to approve (this logs out your current device):\n\n'
                f'    {url}\n\n'
                f'This link expires in {DeviceSwitchToken.EXPIRY_MINUTES} minutes.\n\n'
                f'If you did NOT request this, ignore this email and alert your administrator.\n'
            ),
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[user.email],
            fail_silently=True,
        )
    except Exception as e:
        logger.warning('Switch email failed for %s: %s', user.email, e)


def _send_lockout_email(user, sec):
    try:
        send_mail(
            subject='EasyOffice — Your account has been locked',
            message=(
                f'Hi {user.first_name or user.email},\n\n'
                f'Your account was locked after {sec.max_failed_attempts} failed login attempts.\n'
                f'It will automatically unlock after {sec.lockout_duration_minutes} minutes.\n\n'
                f'If this was not you, contact your administrator immediately.\n'
            ),
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[user.email],
            fail_silently=True,
        )
    except Exception as e:
        logger.warning('Lockout email failed for %s: %s', user.email, e)


def _send_otp_changed_email(target_user, actor_user, enabled, until=None):
    """
    Notify a user that an admin changed their OTP setting.
    `until`: datetime when a bypass expires (only meaningful when enabled=False).
    """
    if not target_user.email:
        return
    try:
        if enabled:
            subject = 'EasyOffice — OTP enabled on your account'
            body = (
                f'Hi {target_user.first_name or target_user.email},\n\n'
                f'OTP (one-time-password verification on login) was just '
                f'enabled on your account by an administrator '
                f'({actor_user.email}).\n\n'
                f'From now on, when you log in from a new or unrecognised '
                f'device, you will receive a 6-digit code by email and must '
                f'enter it before you can sign in.\n\n'
                f'If you did not expect this change, contact your administrator.\n'
            )
        else:
            until_line = ''
            if until:
                until_line = (
                    f'\nThe bypass is temporary and will automatically '
                    f'expire on {until.strftime("%Y-%m-%d at %H:%M UTC")}, '
                    f'after which OTP turns back on automatically.\n'
                )
            subject = 'EasyOffice — OTP temporarily disabled on your account'
            body = (
                f'Hi {target_user.first_name or target_user.email},\n\n'
                f'OTP (one-time-password verification on login) was just '
                f'temporarily disabled on your account by an administrator '
                f'({actor_user.email}).\n\n'
                f'During this window, anyone with your password can log in '
                f'to your account from any device without an email '
                f'verification code.\n'
                f'{until_line}\n'
                f'If you did not expect this change, contact your '
                f'administrator immediately.\n'
            )
        send_mail(
            subject=subject,
            message=body,
            from_email=getattr(settings, 'DEFAULT_FROM_EMAIL', None),
            recipient_list=[target_user.email],
            fail_silently=True,
        )
    except Exception:
        logger.exception('OTP-changed email failed for %s', target_user.email)


def _check_geo_velocity(user, ip, geo, sec):
    if not sec.geo_alert_enabled:
        return
    if not (user.last_login_lat and user.last_login_lng):
        return
    if not (geo.get('lat') and geo.get('lng')):
        return

    mins = (
        (timezone.now() - user.last_seen).total_seconds() / 60
        if user.last_seen else 9999
    )
    if mins > sec.geo_alert_minutes:
        return

    dist = _haversine_km(
        user.last_login_lat, user.last_login_lng,
        geo['lat'], geo['lng']
    )
    if dist < sec.geo_alert_distance_km:
        return

    detail = (
        f"Login from {geo.get('city')}, {geo.get('country')} "
        f"({dist:.0f} km from last login in {user.last_login_city}, "
        f"{user.last_login_country}) within {mins:.0f} minutes."
    )

    SecurityEvent.log(
        user=user,
        event_type=SecurityEvent.EventType.GEO_ALERT,
        ip_address=ip,
        city=geo.get('city', ''),
        country=geo.get('country', ''),
        detail=detail
    )

    _notify_user(
        user,
        title='⚠️ Suspicious login location detected',
        message=(
            f"Login from {geo.get('city')}, {geo.get('country')} — "
            f"{dist:.0f} km away from your last login location in "
            f"{user.last_login_city}, {user.last_login_country}, "
            f"just {mins:.0f} minutes ago. "
            f"If this was not you, contact your administrator immediately."
        ),
        link='/security/'
    )

    _notify_admins(
        title=f'⚠️ Geo-velocity alert: {user.email}',
        message=detail,
        link='/admin/core/securityevent/'
    )


def _update_user_location(user, ip, geo):
    user.last_login_ip = ip or user.last_login_ip
    user.last_login_lat = geo.get('lat') or user.last_login_lat
    user.last_login_lng = geo.get('lng') or user.last_login_lng
    user.last_login_city = geo.get('city', '') or user.last_login_city
    user.last_login_country = geo.get('country', '') or user.last_login_country
    user.save(update_fields=[
        'last_login_ip',
        'last_login_lat',
        'last_login_lng',
        'last_login_city',
        'last_login_country',
    ])


def _complete_login(request, user, device):
    login(request, user)
    user.active_device = device
    user.clear_failed_logins()
    user.update_last_seen()
    user.save(update_fields=['active_device'])
    device.touch()

    SecurityEvent.log(
        user=user,
        event_type=SecurityEvent.EventType.LOGIN_SUCCESS,
        ip_address=_get_client_ip(request),
        device_id=device.device_id,
        device_name=device.device_name,
        city=device.city,
        country=device.country
    )

    next_url = request.GET.get('next') or request.POST.get('next') or '/dashboard/'
    resp = redirect(next_url)
    _set_device_cookie(resp, device.device_id)
    return resp


# ─────────────────────────────────────────────────────────────────────────────
# Login
# ─────────────────────────────────────────────────────────────────────────────

class LoginView(View):
    template_name = 'auth/login.html'

    def get(self, request):
        if request.user.is_authenticated:
            return redirect('dashboard')
        return render(request, self.template_name, {'form': AuthenticationForm()})

    def post(self, request):
        if request.user.is_authenticated:
            return redirect('dashboard')

        form = AuthenticationForm(request, data=request.POST)
        sec = SecuritySettings.get()
        email = request.POST.get('username', '').strip().lower()

        try:
            candidate = User.objects.get(email=email)
        except User.DoesNotExist:
            candidate = None

        if candidate and sec.lockout_enabled and candidate.is_locked_out():
            unlock_str = candidate.lockout_until.strftime('%H:%M UTC')
            return render(request, self.template_name, {
                'form': form,
                'error': (
                    f'Your account is locked. It will unlock at {unlock_str}. '
                    f'Contact your administrator if you need immediate access.'
                ),
            })

        if not form.is_valid():
            if candidate and sec.lockout_enabled:
                now_locked = candidate.record_failed_login(
                    sec.max_failed_attempts,
                    sec.lockout_duration_minutes
                )
                SecurityEvent.log(
                    user=candidate,
                    event_type=SecurityEvent.EventType.LOGIN_FAILED,
                    ip_address=_get_client_ip(request),
                    detail=f'Attempt {candidate.failed_login_count}/{sec.max_failed_attempts}'
                )

                if now_locked:
                    SecurityEvent.log(
                        user=candidate,
                        event_type=SecurityEvent.EventType.ACCOUNT_LOCKED,
                        ip_address=_get_client_ip(request)
                    )
                    _send_lockout_email(candidate, sec)
                    _notify_admins(
                        title=f'🔒 Account locked: {candidate.email}',
                        message=(
                            f'{candidate.email} locked after '
                            f'{sec.max_failed_attempts} failed attempts.'
                        ),
                        link='/admin/core/securityevent/'
                    )
                    return render(request, self.template_name, {
                        'form': form,
                        'error': (
                            f'Your account has been locked after '
                            f'{sec.max_failed_attempts} failed attempts. '
                            f'Check your email for details.'
                        ),
                    })
            return render(request, self.template_name, {'form': form})

        user = form.get_user()
        ip = _get_client_ip(request)
        geo = _geo_lookup(ip)
        device_id = _get_device_id(request)
        device_name = _ua_summary(request)
        city = geo.get('city', '')
        country = geo.get('country', '')

        _check_geo_velocity(user, ip, geo, sec)
        _update_user_location(user, ip, geo)

        trusted = None
        if device_id:
            trusted = TrustedDevice.objects.filter(
                user=user,
                device_id=device_id
            ).first()

        # If a superuser has forced OTP for this login (one-shot or sticky),
        # treat the device as untrusted so we fall through to the OTP flow.
        # We don't touch the trusted-device row itself — once OTP is verified
        # the user can keep using the device normally.
        if trusted and trusted.is_valid() and user.otp_required_this_login:
            reason = (
                'admin set "always require OTP"'
                if user.otp_always_required
                else 'admin set "force OTP on next login"'
            )
            SecurityEvent.log(
                user=user,
                event_type=SecurityEvent.EventType.UNTRUSTED_ATTEMPT,
                ip_address=ip,
                device_id=device_id,
                device_name=device_name,
                city=city,
                country=country,
                detail=(
                    f'Trusted device, but OTP forced by admin policy '
                    f'({reason}). Falling through to OTP challenge.'
                ),
            )
            trusted = None

        if trusted and trusted.is_valid():
            current = user.active_device

            if current is None or current.pk == trusted.pk:
                return _complete_login(request, user, trusted)

            # Trusted device, but a different device is active.
            # If OTP is disabled, allow the switch silently — otherwise email
            # the one-click switch link.
            if not user.otp_enabled:
                old = current
                old.is_revoked = True
                old.save(update_fields=['is_revoked'])
                SecurityEvent.log(
                    user=user,
                    event_type=SecurityEvent.EventType.DEVICE_REVOKED,
                    device_id=old.device_id,
                    device_name=old.device_name,
                    detail='Auto-revoked: switching device for user with OTP disabled.',
                )
                _notify_admins(
                    title=f'⚠️ Device switched without OTP: {user.email}',
                    message=(
                        f'{user.email} switched login from "{old.device_name}" '
                        f'to "{device_name}" ({city}, {country}, {ip}) '
                        f'without OTP verification (OTP is disabled for this user).'
                    ),
                    link='/admin/core/securityevent/'
                )
                return _complete_login(request, user, trusted)

            sw = DeviceSwitchToken.issue(
                user=user,
                new_device_id=device_id,
                new_device_name=device_name,
                new_ip=ip,
                new_city=city,
                new_country=country,
                new_user_agent=request.META.get('HTTP_USER_AGENT', ''),
            )
            _send_switch_email(user, sw, request)

            SecurityEvent.log(
                user=user,
                event_type=SecurityEvent.EventType.SWITCH_REQUESTED,
                ip_address=ip,
                device_id=device_id,
                device_name=device_name,
                city=city,
                country=country
            )
            request.session[SESSION_SWITCH] = str(sw.pk)
            return render(request, 'auth/switch_pending.html', {
                'user_email': user.email,
                'device_name': device_name,
                'current_device': current.device_name if current else 'another device',
            })

        # ── Unrecognised device path ─────────────────────────────────────────
        current = user.active_device

        # If the user has OTP disabled, skip the whole challenge entirely.
        # We still log/notify on untrusted-device attempts so admins can see
        # them. We trust the new device immediately (since OTP is off for this
        # user, we can't ask them to verify it anyway), then complete login.
        if not user.otp_enabled:
            if current and current.is_valid():
                _notify_admins(
                    title=f'⚠️ Untrusted device attempt: {user.email}',
                    message=(
                        f'{user.email}: untrusted "{device_name}" from '
                        f'{city}, {country} ({ip}) — OTP is disabled for this user.'
                    ),
                    link='/admin/core/securityevent/'
                )
                SecurityEvent.log(
                    user=user,
                    event_type=SecurityEvent.EventType.UNTRUSTED_ATTEMPT,
                    ip_address=ip,
                    device_id=device_id,
                    device_name=device_name,
                    city=city,
                    country=country,
                    detail='OTP disabled — login allowed without verification.',
                )
                # Revoke the old session, this device replaces it.
                current.is_revoked = True
                current.save(update_fields=['is_revoked'])
                SecurityEvent.log(
                    user=user,
                    event_type=SecurityEvent.EventType.DEVICE_REVOKED,
                    device_id=current.device_id,
                    device_name=current.device_name,
                    detail='Auto-revoked: replaced by new login (OTP disabled).',
                )

            pending_did = device_id or _new_device_id()
            new_device = TrustedDevice.create_for(
                user=user,
                device_id=pending_did,
                device_name=device_name,
                user_agent=request.META.get('HTTP_USER_AGENT', ''),
                ip_address=ip,
                city=city,
                country=country,
            )
            response = _complete_login(request, user, new_device)
            _set_device_cookie(response, pending_did)
            return response

        # ── Otherwise, fall through to the normal OTP flow ──────────────────
        if current and current.is_valid():
            purpose = OTPChallenge.Purpose.UNTRUSTED_WARN
            _notify_user(
                user,
                title='⚠️ Login attempt from unrecognised device',
                message=(
                    f'An untrusted device ({device_name}) tried to log in from '
                    f'{city or ip}. If this is not you, contact your administrator. '
                    f'If it is you, use the OTP sent to your email.'
                ),
                link='/security/'
            )
            _notify_admins(
                title=f'⚠️ Untrusted device attempt: {user.email}',
                message=(
                    f'{user.email}: untrusted "{device_name}" from '
                    f'{city}, {country} ({ip}) — active session exists.'
                ),
                link='/admin/core/securityevent/'
            )
            SecurityEvent.log(
                user=user,
                event_type=SecurityEvent.EventType.UNTRUSTED_ATTEMPT,
                ip_address=ip,
                device_id=device_id,
                device_name=device_name,
                city=city,
                country=country
            )
        else:
            purpose = OTPChallenge.Purpose.DEVICE_VERIFY

        pending_did = device_id or _new_device_id()
        request.session['pending_device_id'] = pending_did

        otp = OTPChallenge.issue(
            user=user,
            purpose=purpose,
            pending_device_id=pending_did,
            pending_device_name=device_name,
            pending_ip=ip,
            pending_city=city,
            pending_country=country,
            pending_user_agent=request.META.get('HTTP_USER_AGENT', ''),
        )
        email_sent = _send_otp_email(user, otp)

        if not email_sent:
            messages.error(
                request,
                "We generated your OTP, but the email could not be sent. Please contact the administrator."
            )
            logger.error("OTP created but email failed for user %s", user.email)

        SecurityEvent.log(
            user=user,
            event_type=SecurityEvent.EventType.OTP_SENT,
            ip_address=ip,
            device_id=pending_did,
            device_name=device_name,
            city=city,
            country=country
        )

        request.session[SESSION_OTP_ID] = str(otp.pk)
        request.session['_otp_user_id'] = str(user.pk)

        return render(request, 'auth/otp_verify.html', {
            'purpose': purpose,
            'user_email': user.email,
            'is_untrusted_warn': purpose == OTPChallenge.Purpose.UNTRUSTED_WARN,
            'attempts_left': OTPChallenge.MAX_ATTEMPTS,
        })


# ─────────────────────────────────────────────────────────────────────────────
# OTP Verify
# ─────────────────────────────────────────────────────────────────────────────

class OTPVerifyView(View):
    template_name = 'auth/otp_verify.html'

    def _get_otp(self, request):
        pk = request.session.get(SESSION_OTP_ID)
        if not pk:
            return None
        try:
            return OTPChallenge.objects.select_related('user').get(pk=pk)
        except OTPChallenge.DoesNotExist:
            return None

    def get(self, request):
        otp = self._get_otp(request)
        if not otp:
            messages.error(request, 'Session expired. Please log in again.')
            return redirect('login')

        return render(request, self.template_name, {
            'user_email': otp.user.email,
            'purpose': otp.purpose,
            'is_untrusted_warn': otp.purpose == OTPChallenge.Purpose.UNTRUSTED_WARN,
            'attempts_left': OTPChallenge.MAX_ATTEMPTS - otp.attempts,
        })

    def post(self, request):
        otp = self._get_otp(request)
        if not otp:
            messages.error(request, 'Session expired. Please log in again.')
            return redirect('login')

        user = otp.user
        code = request.POST.get('otp_code', '').strip()

        if not otp.is_valid():
            SecurityEvent.log(
                user=user,
                event_type=SecurityEvent.EventType.OTP_EXPIRED,
                ip_address=_get_client_ip(request)
            )
            messages.error(request, 'This code has expired. Please log in again.')
            request.session.pop(SESSION_OTP_ID, None)
            return redirect('login')

        if not otp.verify(code):
            SecurityEvent.log(
                user=user,
                event_type=SecurityEvent.EventType.OTP_FAILED,
                ip_address=_get_client_ip(request)
            )
            left = OTPChallenge.MAX_ATTEMPTS - otp.attempts
            if left <= 0:
                messages.error(request, 'Too many wrong attempts. Please log in again.')
                request.session.pop(SESSION_OTP_ID, None)
                return redirect('login')

            return render(request, self.template_name, {
                'user_email': user.email,
                'purpose': otp.purpose,
                'is_untrusted_warn': otp.purpose == OTPChallenge.Purpose.UNTRUSTED_WARN,
                'attempts_left': left,
                'error': f'Incorrect code. {left} attempt(s) remaining.',
            })

        SecurityEvent.log(
            user=user,
            event_type=SecurityEvent.EventType.OTP_SUCCESS,
            ip_address=_get_client_ip(request),
            device_id=otp.pending_device_id,
            device_name=otp.pending_device_name
        )

        # If a superuser had forced OTP for "next login", that requirement
        # is now satisfied — clear it so the next login on a trusted device
        # goes through the normal fast path again. The sticky
        # `otp_always_required` flag is intentionally NOT cleared here.
        user.consume_force_otp_flag()

        sec = SecuritySettings.get()

        device = TrustedDevice.create_for(
            user=user,
            device_id=otp.pending_device_id or request.session.get('pending_device_id') or _new_device_id(),
            device_name=otp.pending_device_name,
            user_agent=otp.pending_user_agent,
            ip_address=otp.pending_ip,
            city=otp.pending_city,
            country=otp.pending_country,
        )
        device.expires_at = timezone.now() + timedelta(days=sec.device_trust_days)
        device.save(update_fields=['expires_at'])

        SecurityEvent.log(
            user=user,
            event_type=SecurityEvent.EventType.DEVICE_TRUSTED,
            device_id=device.device_id,
            device_name=device.device_name,
            city=device.city,
            country=device.country
        )

        request.session.pop(SESSION_OTP_ID, None)
        request.session.pop('pending_device_id', None)
        return _complete_login(request, user, device)


# ─────────────────────────────────────────────────────────────────────────────
# Resend OTP
# ─────────────────────────────────────────────────────────────────────────────

class ResendOTPView(View):
    def post(self, request):
        pk = request.session.get(SESSION_OTP_ID)
        if not pk:
            return redirect('login')

        try:
            old = OTPChallenge.objects.select_related('user').get(pk=pk)
        except OTPChallenge.DoesNotExist:
            return redirect('login')

        new_otp = OTPChallenge.issue(
            user=old.user,
            purpose=old.purpose,
            pending_device_id=old.pending_device_id or request.session.get('pending_device_id'),
            pending_device_name=old.pending_device_name,
            pending_ip=old.pending_ip,
            pending_city=old.pending_city,
            pending_country=old.pending_country,
            pending_user_agent=old.pending_user_agent,
        )
        _send_otp_email(old.user, new_otp)
        request.session[SESSION_OTP_ID] = str(new_otp.pk)
        messages.success(request, 'A new code has been sent to your email.')
        return redirect('otp_verify')


# ─────────────────────────────────────────────────────────────────────────────
# Device Switch Approval
# ─────────────────────────────────────────────────────────────────────────────

class DeviceSwitchApproveView(View):
    def get(self, request, token):
        sw = get_object_or_404(DeviceSwitchToken, token=token)
        if not sw.is_valid():
            return render(request, 'auth/switch_expired.html', {})

        user = sw.user

        if user.active_device:
            old = user.active_device
            old.is_revoked = True
            old.save(update_fields=['is_revoked'])
            SecurityEvent.log(
                user=user,
                event_type=SecurityEvent.EventType.DEVICE_REVOKED,
                device_id=old.device_id,
                device_name=old.device_name
            )

        new_device = TrustedDevice.create_for(
            user=user,
            device_id=sw.new_device_id,
            device_name=sw.new_device_name,
            user_agent=sw.new_user_agent,
            ip_address=sw.new_ip,
            city=sw.new_city,
            country=sw.new_country,
        )
        sw.is_used = True
        sw.save(update_fields=['is_used'])

        SecurityEvent.log(
            user=user,
            event_type=SecurityEvent.EventType.SWITCH_APPROVED,
            device_id=new_device.device_id,
            device_name=new_device.device_name,
            city=new_device.city,
            country=new_device.country
        )

        login(request, user)
        user.active_device = new_device
        user.update_last_seen()
        user.save(update_fields=['active_device'])
        new_device.touch()

        resp = redirect('/dashboard/')
        _set_device_cookie(resp, new_device.device_id)
        messages.success(request, 'Device switch approved. You are now logged in.')
        return resp


class SwitchPendingView(View):
    def get(self, request):
        return render(request, 'auth/switch_pending.html', {})


# ─────────────────────────────────────────────────────────────────────────────
# Logout
# ─────────────────────────────────────────────────────────────────────────────

class LogoutView(LoginRequiredMixin, View):
    def _do_logout(self, request):
        user = request.user
        if user.is_authenticated:
            SecurityEvent.log(
                user=user,
                event_type=SecurityEvent.EventType.LOGOUT,
                ip_address=_get_client_ip(request)
            )
            user.is_online = False
            user.active_device = None
            user.save(update_fields=['is_online', 'active_device'])

        logout(request)
        resp = redirect('login')
        resp.delete_cookie(DEVICE_COOKIE)
        return resp

    def post(self, request):
        return self._do_logout(request)

    def get(self, request):
        return self._do_logout(request)


# ─────────────────────────────────────────────────────────────────────────────
# Trusted Devices
# ─────────────────────────────────────────────────────────────────────────────

class TrustedDevicesView(LoginRequiredMixin, View):
    template_name = 'auth/trusted_devices.html'

    def get(self, request):
        current_cookie_device_id = _get_device_id(request)
        return render(request, self.template_name, {
            'devices': request.user.trusted_devices.order_by('-trusted_at'),
            'current_cookie_device_id': current_cookie_device_id,
            'current_active_device_pk': request.user.active_device_id,
        })

    def post(self, request):
        action = request.POST.get('action')
        did = request.POST.get('device_id')

        if action == 'revoke' and did:
            try:
                device = TrustedDevice.objects.get(pk=did, user=request.user)
                device.is_revoked = True
                device.save(update_fields=['is_revoked'])
                if request.user.active_device_id == device.pk:
                    request.user.active_device = None
                    request.user.save(update_fields=['active_device'])
                SecurityEvent.log(
                    user=request.user,
                    event_type=SecurityEvent.EventType.DEVICE_REVOKED,
                    device_id=device.device_id,
                    device_name=device.device_name
                )
                messages.success(request, f'"{device.device_name}" has been revoked.')
            except TrustedDevice.DoesNotExist:
                messages.error(request, 'Device not found.')

        elif action == 'revoke_all':
            request.user.trusted_devices.update(is_revoked=True)
            request.user.active_device = None
            request.user.save(update_fields=['active_device'])
            SecurityEvent.log(
                user=request.user,
                event_type=SecurityEvent.EventType.DEVICE_REVOKED,
                detail='All devices revoked by user.'
            )
            messages.success(request, 'All trusted devices have been revoked.')

        return redirect('trusted_devices')


# ─────────────────────────────────────────────────────────────────────────────
# Security Dashboard (read-only — no OTP toggle here, that's superuser-only)
# ─────────────────────────────────────────────────────────────────────────────

class SecurityDashboardView(LoginRequiredMixin, TemplateView):
    template_name = 'auth/security.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        user = self.request.user
        ctx['events'] = user.security_events.all()[:50]
        ctx['trusted_devices'] = user.trusted_devices.filter(
            is_revoked=False
        ).order_by('-trusted_at')
        ctx['current_cookie_device_id'] = _get_device_id(self.request)
        ctx['current_active_device_pk'] = user.active_device_id
        return ctx


# ─────────────────────────────────────────────────────────────────────────────
# OTP enable / disable — SUPERUSER ONLY, target user passed in URL
# ─────────────────────────────────────────────────────────────────────────────

class ToggleOTPView(LoginRequiredMixin, View):
    """
    Superuser-only. Lets a superuser temporarily disable OTP on a user's
    account, or re-enable it.

    POST /otp/toggle/<uuid:user_id>/
        action          : 'enable' or 'disable'
        duration_hours  : (disable only) integer hours, 1..168 (max 7 days)
        next            : (optional) redirect target after the action

    Disabling is always TIME-LIMITED. The bypass automatically expires after
    the duration elapses, at which point OTP turns back on without any
    further admin action. The maximum duration is 7 days (168 hours).

    Non-superusers get 403. Self-toggle is blocked too — superusers cannot
    use this endpoint to disable OTP on their own account (they'd have to do
    it via Django admin or the shell). This prevents an attacker who
    compromises a superuser session from silently disabling OTP on it.
    """

    def post(self, request, user_id):
        actor = request.user

        if not actor.is_superuser:
            return HttpResponseForbidden(
                'Only superusers can change OTP settings for users.'
            )

        target = get_object_or_404(User, pk=user_id)
        action = request.POST.get('action', '').strip().lower()

        if action not in ('enable', 'disable'):
            messages.error(request, 'Invalid action.')
            return self._safe_redirect(request, target)

        if target.pk == actor.pk:
            messages.error(
                request,
                'For your own safety, you cannot toggle OTP on your own '
                'account from here. Use the Django admin if you need to.'
            )
            return self._safe_redirect(request, target)

        # ── ENABLE ──────────────────────────────────────────────────────────
        if action == 'enable':
            if target.otp_enabled:
                messages.info(request, f'OTP is already enabled for {target.email}.')
                return self._safe_redirect(request, target)

            target.enable_otp()

            SecurityEvent.log(
                user=target,
                event_type=SecurityEvent.EventType.OTP_ENABLED,
                ip_address=_get_client_ip(request),
                detail=f'OTP bypass cleared (re-enabled) by superuser {actor.email}.',
            )
            _notify_admins(
                title=f'⚙️ OTP re-enabled for {target.email}',
                message=f'{actor.email} re-enabled OTP for {target.email}.',
                link='/admin/core/securityevent/'
            )
            _notify_user(
                target,
                title='OTP re-enabled on your account',
                message=(
                    f'An administrator ({actor.email}) re-enabled OTP on your '
                    f'account. You will be asked for an email verification '
                    f'code on your next login from a new device.'
                ),
                link='/security/'
            )
            _send_otp_changed_email(target, actor, enabled=True)

            messages.success(
                request,
                f'OTP has been re-enabled for {target.email}.'
            )
            return self._safe_redirect(request, target)

        # ── DISABLE (time-limited bypass) ───────────────────────────────────
        # Required: duration_hours, an integer in [OTP_BYPASS_MIN_HOURS,
        # OTP_BYPASS_MAX_HOURS]. The form normally posts a preset (1, 24,
        # 72, 168) but a custom value is allowed within the cap.
        try:
            hours = int(request.POST.get('duration_hours', '').strip())
        except (TypeError, ValueError):
            messages.error(
                request,
                'Please choose how long OTP should remain disabled '
                f'(between {OTP_BYPASS_MIN_HOURS} hour and '
                f'{OTP_BYPASS_MAX_HOURS} hours / 7 days).'
            )
            return self._safe_redirect(request, target)

        if hours < OTP_BYPASS_MIN_HOURS or hours > OTP_BYPASS_MAX_HOURS:
            messages.error(
                request,
                f'OTP can only be disabled for between {OTP_BYPASS_MIN_HOURS} '
                f'hour and {OTP_BYPASS_MAX_HOURS} hours (7 days). '
                f'Got {hours}.'
            )
            return self._safe_redirect(request, target)

        new_until = timezone.now() + timedelta(hours=hours)

        # If a bypass is already in effect, treat the new request as an
        # extension/replacement, not an error. We still log it.
        was_already_bypassed = target.otp_bypass_active

        target.disable_otp_until(new_until)

        # Human-friendly duration label
        if hours >= 24:
            days = hours / 24
            duration_label = (
                f'{int(days)} days' if days.is_integer() else f'{days:.1f} days'
            )
        elif hours == 1:
            duration_label = '1 hour'
        else:
            duration_label = f'{hours} hours'

        until_label = new_until.strftime('%Y-%m-%d %H:%M UTC')
        verb = 'extended' if was_already_bypassed else 'temporarily disabled'

        SecurityEvent.log(
            user=target,
            event_type=SecurityEvent.EventType.OTP_DISABLED,
            ip_address=_get_client_ip(request),
            detail=(
                f'OTP {verb} by superuser {actor.email} for {duration_label} '
                f'(until {until_label}).'
            ),
        )
        _notify_admins(
            title=f'⚠️ OTP {verb} for {target.email}',
            message=(
                f'{actor.email} {verb} OTP for {target.email} for '
                f'{duration_label} (until {until_label}). Logins from new '
                f'devices for this user no longer require email verification '
                f'until then.'
            ),
            link='/admin/core/securityevent/'
        )
        _notify_user(
            target,
            title='OTP temporarily disabled on your account',
            message=(
                f'An administrator ({actor.email}) {verb} OTP on your '
                f'account for {duration_label}. Until {until_label}, logins '
                f'from new devices will not require an email verification '
                f'code. OTP will turn back on automatically after that. '
                f'If you did not expect this change, contact your '
                f'administrator immediately.'
            ),
            link='/security/'
        )
        _send_otp_changed_email(target, actor, enabled=False, until=new_until)

        messages.warning(
            request,
            f'OTP has been {verb} for {target.email} for {duration_label} '
            f'(until {until_label}). It will automatically re-enable after that.'
        )
        return self._safe_redirect(request, target)


    @staticmethod
    def _safe_redirect(request, target):
        nxt = request.POST.get('next', '')
        if nxt and nxt.startswith('/'):
            return redirect(nxt)
        # Try the staff detail page; fall back to dashboard if not configured.
        try:
            return redirect('staff_detail', pk=target.pk)
        except Exception:
            return redirect('/dashboard/')


# ─────────────────────────────────────────────────────────────────────────────
# Superuser-only device & OTP enforcement endpoints
# ─────────────────────────────────────────────────────────────────────────────

def _safe_redirect_for(request, target, fallback='/dashboard/'):
    """Shared redirect helper for the superuser-only endpoints below."""
    nxt = request.POST.get('next', '') or request.GET.get('next', '')
    if nxt and nxt.startswith('/'):
        return redirect(nxt)
    try:
        return redirect('staff_detail', pk=target.pk)
    except Exception:
        return redirect(fallback)


class _SuperuserActionMixin(LoginRequiredMixin):
    """Tiny mixin: 403 unless request.user.is_superuser."""

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect('login')
        if not request.user.is_superuser:
            return HttpResponseForbidden(
                'Only superusers can perform this action.'
            )
        return super().dispatch(request, *args, **kwargs)


class ResetTrustedDevicesView(_SuperuserActionMixin, View):
    """
    Superuser-only. Revokes every trusted device for the target user and
    detaches their active_device pointer. The user keeps their current
    browser session (use ForceLogoutView for that), but on their next
    login from any device they will go through the OTP flow again.

    POST /admin/users/<uuid:user_id>/reset-devices/
        next : (optional) redirect target
    """

    def post(self, request, user_id):
        actor = request.user
        target = get_object_or_404(User, pk=user_id)

        n = target.revoke_all_trusted_devices()

        SecurityEvent.log(
            user=target,
            event_type=SecurityEvent.EventType.DEVICES_RESET,
            ip_address=_get_client_ip(request),
            detail=(
                f'{n} trusted device(s) revoked by superuser {actor.email}. '
                f'User will be re-challenged on next login from any device.'
            ),
        )
        _notify_admins(
            title=f'🔐 Trusted devices reset: {target.email}',
            message=(
                f'{actor.email} reset all trusted devices for {target.email} '
                f'({n} device(s) revoked). Next login from any device will '
                f'go through OTP verification.'
            ),
            link='/admin/core/securityevent/',
        )
        _notify_user(
            target,
            title='Your trusted devices were reset',
            message=(
                f'An administrator ({actor.email}) revoked all trusted '
                f'devices on your account. The next time you log in — even '
                f'from a device you were using before — you will be asked '
                f'for a 6-digit code emailed to you.'
            ),
            link='/security/',
        )

        if n:
            messages.success(
                request,
                f'Reset complete — {n} trusted device(s) revoked for '
                f'{target.email}. They will go through OTP on next login.'
            )
        else:
            messages.info(
                request,
                f'{target.email} had no active trusted devices to reset.'
            )
        return _safe_redirect_for(request, target)


class ForceLogoutView(_SuperuserActionMixin, View):
    """
    Superuser-only. Kills every active Django session belonging to the
    target user, detaches their active_device pointer, and marks them
    offline. On their next request they will be bounced back to the
    login page.

    POST /admin/users/<uuid:user_id>/force-logout/
        next : (optional) redirect target
    """

    def post(self, request, user_id):
        actor = request.user
        target = get_object_or_404(User, pk=user_id)

        if target.pk == actor.pk:
            messages.error(
                request,
                'For your own safety, you cannot force-logout your own '
                'account from this page. Use the regular Logout link.'
            )
            return _safe_redirect_for(request, target)

        killed = target.force_logout_everywhere()

        SecurityEvent.log(
            user=target,
            event_type=SecurityEvent.EventType.FORCED_LOGOUT,
            ip_address=_get_client_ip(request),
            detail=(
                f'Force logout by superuser {actor.email}: {killed} '
                f'session(s) destroyed.'
            ),
        )
        _notify_admins(
            title=f'🚪 Forced logout: {target.email}',
            message=(
                f'{actor.email} forced logout for {target.email} — '
                f'{killed} session(s) terminated. Active device pointer '
                f'cleared.'
            ),
            link='/admin/core/securityevent/',
        )
        _notify_user(
            target,
            title='You were signed out by an administrator',
            message=(
                f'An administrator ({actor.email}) signed you out of all '
                f'your active sessions. You will need to log in again. '
                f'If you did not expect this, contact your administrator.'
            ),
            link='/security/',
        )

        messages.success(
            request,
            f'Force logout complete for {target.email} — '
            f'{killed} session(s) terminated.'
        )
        return _safe_redirect_for(request, target)


class ForceOTPNextLoginView(_SuperuserActionMixin, View):
    """
    Superuser-only. Sets / clears the one-shot "force OTP on next login"
    flag. While the flag is set, the trusted-device fast path is skipped
    on the user's next login attempt and they will be challenged with an
    emailed 6-digit code. The flag clears itself after one successful
    OTP verification.

    POST /admin/users/<uuid:user_id>/force-otp-next/
        action : 'set' or 'clear'
        next   : (optional) redirect target
    """

    def post(self, request, user_id):
        actor = request.user
        target = get_object_or_404(User, pk=user_id)
        action = request.POST.get('action', '').strip().lower()

        if action not in ('set', 'clear'):
            messages.error(request, 'Invalid action.')
            return _safe_redirect_for(request, target)

        if action == 'set':
            if not target.otp_enabled:
                messages.warning(
                    request,
                    f'Cannot force OTP for {target.email}: OTP is currently '
                    f'bypassed for this user (until '
                    f'{target.otp_disabled_until:%Y-%m-%d %H:%M UTC}). '
                    f'Re-enable OTP first.'
                )
                return _safe_redirect_for(request, target)

            target.set_force_otp_next_login(True)

            SecurityEvent.log(
                user=target,
                event_type=SecurityEvent.EventType.OTP_FORCED_NEXT,
                ip_address=_get_client_ip(request),
                detail=(
                    f'Force-OTP-on-next-login set by superuser {actor.email}. '
                    f'Will fire once and auto-clear.'
                ),
            )
            _notify_admins(
                title=f'🔒 OTP forced on next login: {target.email}',
                message=(
                    f'{actor.email} marked {target.email} for one-shot OTP '
                    f'on their next login attempt (even on trusted devices).'
                ),
                link='/admin/core/securityevent/',
            )
            _notify_user(
                target,
                title='OTP required on your next login',
                message=(
                    f'An administrator ({actor.email}) flagged your account '
                    f'so that your next login requires a 6-digit email '
                    f'verification code, even from a device you have used '
                    f'before. After one successful verification, normal '
                    f'behaviour resumes.'
                ),
                link='/security/',
            )
            messages.success(
                request,
                f'OTP will be required on the next login for {target.email}.'
            )
        else:  # clear
            if not target.force_otp_next_login:
                messages.info(
                    request,
                    f'No force-OTP flag was set for {target.email}.'
                )
                return _safe_redirect_for(request, target)

            target.set_force_otp_next_login(False)

            SecurityEvent.log(
                user=target,
                event_type=SecurityEvent.EventType.OTP_FORCED_NEXT,
                ip_address=_get_client_ip(request),
                detail=(
                    f'Force-OTP-on-next-login CLEARED by superuser '
                    f'{actor.email} (manual revert).'
                ),
            )
            messages.success(
                request,
                f'Force-OTP flag cleared for {target.email}.'
            )

        return _safe_redirect_for(request, target)


class OTPAlwaysRequiredView(_SuperuserActionMixin, View):
    """
    Superuser-only. Toggles the sticky 'OTP every login' requirement.
    While ON, the trusted-device fast path is permanently disabled for
    this user — every login from every device requires an emailed code.

    POST /admin/users/<uuid:user_id>/otp-always/
        action : 'on' or 'off'
        next   : (optional) redirect target
    """

    def post(self, request, user_id):
        actor = request.user
        target = get_object_or_404(User, pk=user_id)
        action = request.POST.get('action', '').strip().lower()

        if action not in ('on', 'off'):
            messages.error(request, 'Invalid action.')
            return _safe_redirect_for(request, target)

        new_value = (action == 'on')
        if target.otp_always_required == new_value:
            messages.info(
                request,
                f'Always-require-OTP is already '
                f'{"on" if new_value else "off"} for {target.email}.'
            )
            return _safe_redirect_for(request, target)

        if new_value and not target.otp_enabled:
            messages.warning(
                request,
                f'Cannot turn on always-require-OTP for {target.email}: '
                f'OTP is currently bypassed for this user. Re-enable OTP '
                f'first.'
            )
            return _safe_redirect_for(request, target)

        target.set_otp_always_required(new_value)

        ev_type = (SecurityEvent.EventType.OTP_ALWAYS_ON if new_value
                   else SecurityEvent.EventType.OTP_ALWAYS_OFF)
        SecurityEvent.log(
            user=target,
            event_type=ev_type,
            ip_address=_get_client_ip(request),
            detail=(
                f'Always-require-OTP turned '
                f'{"ON" if new_value else "OFF"} by superuser {actor.email}.'
            ),
        )
        _notify_admins(
            title=(
                f'🔐 Always-require-OTP {"enabled" if new_value else "disabled"}: '
                f'{target.email}'
            ),
            message=(
                f'{actor.email} turned always-require-OTP '
                f'{"ON" if new_value else "OFF"} for {target.email}. '
                + ('Every login will now require an emailed code, even on '
                   'trusted devices.' if new_value else
                   'Trusted-device fast path restored to normal behaviour.')
            ),
            link='/admin/core/securityevent/',
        )
        _notify_user(
            target,
            title=(
                'OTP-on-every-login turned '
                f'{"on" if new_value else "off"}'
            ),
            message=(
                f'An administrator ({actor.email}) '
                + ('enabled OTP on every login for your account. From now '
                   'on, every time you sign in — even on a device you have '
                   'used before — you will need a 6-digit code emailed to '
                   'you.'
                   if new_value else
                   'turned off the always-require-OTP setting on your '
                   'account. Trusted devices will once again skip the OTP '
                   'challenge.')
            ),
            link='/security/',
        )
        messages.success(
            request,
            f'Always-require-OTP is now '
            f'{"ON" if new_value else "OFF"} for {target.email}.'
        )
        return _safe_redirect_for(request, target)


# ─────────────────────────────────────────────────────────────────────────────
# Profile / other views
# ─────────────────────────────────────────────────────────────────────────────

class ProfileView(LoginRequiredMixin, TemplateView):
    template_name = 'auth/profile.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        user = self.request.user
        ctx['recent_tasks'] = user.assigned_tasks.filter(
            status__in=['todo', 'in_progress']
        ).order_by('-created_at')[:5]
        ctx['recent_projects'] = user.projects.filter(
            status='active'
        ).order_by('-created_at')[:5]
        raw = getattr(user, 'skills', '') or ''
        ctx['staff_skills'] = [s.strip() for s in raw.split(',') if s.strip()]
        return ctx


class ProfileEditView(LoginRequiredMixin, View):
    template_name = 'auth/profile_edit.html'

    def get(self, request):
        return render(request, self.template_name, {'user': request.user})

    def post(self, request):
        u = request.user
        u.first_name = request.POST.get('first_name', u.first_name)
        u.last_name = request.POST.get('last_name', u.last_name)
        u.phone = request.POST.get('phone', u.phone)
        u.bio = request.POST.get('bio', u.bio)
        u.linkedin_url = request.POST.get('linkedin_url', u.linkedin_url)
        u.skills = request.POST.get('skills', u.skills)
        u.timezone_pref = request.POST.get('timezone_pref', u.timezone_pref)
        u.theme = request.POST.get('theme', u.theme)
        u.notification_email = 'notification_email' in request.POST
        u.notification_push = 'notification_push' in request.POST

        if 'avatar' in request.FILES:
            u.avatar = request.FILES['avatar']

        u.save()
        messages.success(request, 'Profile updated successfully.')
        return redirect('profile')


class ChangePasswordView(LoginRequiredMixin, View):
    template_name = 'auth/change_password.html'

    def get(self, request):
        return render(
            request,
            self.template_name,
            {'form': PasswordChangeForm(request.user)}
        )

    def post(self, request):
        form = PasswordChangeForm(request.user, request.POST)
        if form.is_valid():
            user = form.save()
            update_session_auth_hash(request, user)
            messages.success(request, 'Password changed successfully.')
            return redirect('profile')
        return render(request, self.template_name, {'form': form})


class UserSettingsView(LoginRequiredMixin, View):
    template_name = 'auth/settings.html'

    def get(self, request):
        return render(request, self.template_name)

    def post(self, request):
        u = request.user
        u.theme = request.POST.get('theme', u.theme)
        u.compact_view = 'compact_view' in request.POST
        u.notification_email = 'notification_email' in request.POST
        u.notification_push = 'notification_push' in request.POST
        u.language = request.POST.get('language', u.language)
        u.timezone_pref = request.POST.get('timezone_pref', u.timezone_pref)
        u.save()
        messages.success(request, 'Settings saved.')
        return redirect('user_settings')


class NotificationsView(LoginRequiredMixin, TemplateView):
    template_name = 'auth/notifications.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['notifications'] = self.request.user.core_notifications.all()[:50]
        return ctx


class MarkNotificationsReadView(LoginRequiredMixin, View):
    def post(self, request):
        updated = request.user.core_notifications.filter(is_read=False).update(
            is_read=True,
            read_at=timezone.now()
        )

        unread_count = request.user.core_notifications.filter(is_read=False).count()

        if request.headers.get('x-requested-with') == 'XMLHttpRequest':
            return JsonResponse({
                'status': 'ok',
                'marked_read': updated,
                'unread_count': unread_count,
            })

        next_url = request.POST.get('next') or request.META.get('HTTP_REFERER') or '/dashboard/'
        return redirect(next_url)


class MarkNotificationReadView(LoginRequiredMixin, View):
    def post(self, request, pk):
        notif = get_object_or_404(
            CoreNotification,
            pk=pk,
            recipient=request.user,
        )

        if not notif.is_read:
            notif.is_read = True
            notif.read_at = timezone.now()
            notif.save(update_fields=['is_read', 'read_at'])

        unread_count = request.user.core_notifications.filter(is_read=False).count()

        if request.headers.get('x-requested-with') == 'XMLHttpRequest':
            return JsonResponse({
                'status': 'ok',
                'notification_id': str(notif.pk),
                'unread_count': unread_count,
            })

        next_url = request.POST.get('next') or notif.link or request.META.get('HTTP_REFERER') or '/dashboard/'
        return redirect(next_url)


# ─────────────────────────────────────────────────────────────────────────────
# Notifications Bell — JSON endpoint for auto-refresh polling
# ─────────────────────────────────────────────────────────────────────────────

class NotificationsBellView(LoginRequiredMixin, View):
    """
    GET /notifications/bell/
    Returns the latest unread notifications as JSON for the bell dropdown.
    Used by the 30-second polling loop and the on-open refresh in base.html.
    """
    def get(self, request):
        if request.headers.get('x-requested-with') != 'XMLHttpRequest':
            return redirect('notifications')

        unread_qs = request.user.core_notifications.filter(is_read=False)
        # True total unread count (the badge shows this).
        unread_count = unread_qs.count()
        # Only the latest 5 items are surfaced in the dropdown UI.
        latest = unread_qs.order_by('-created_at')[:5]

        notifications = [
            {
                'id':       str(n.pk),
                'read_url': reverse('mark_notification_read', kwargs={'pk': n.pk}),
                'type':     n.notification_type,
                'title':    n.title,
                'body':     n.message,
                'link':     n.link or '#',
                'actions':  (n.data or {}).get('actions', []),
            }
            for n in latest
        ]

        return JsonResponse({
            'unread_count':  unread_count,
            'notifications': notifications,
        })