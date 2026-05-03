"""
apps/admin_security/views.py
────────────────────────────
Three things live here:

    settings_view
        Superuser-only page to toggle `is_admin_enabled` and
        `require_otp_for_admin`. Includes a safety check that prevents
        the *current* superuser from disabling admin AND simultaneously
        not being recorded as `last_changed_by` (which would lock them
        out — we always set it to request.user when they save).

    otp_challenge_view
        Where the OTP gate middleware sends authenticated users when
        they need to enter their 6-digit code. Issues a fresh code on
        first GET, accepts the code on POST, and stamps the session as
        verified on success.

    resend_otp_view
        Issues a new code (rate-limited to one every 30 seconds via
        a session timestamp).

The user_logged_in signal handler is in signals.py — it issues the first
OTP automatically when an admin user finishes the password step.
"""
from __future__ import annotations

import logging

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.core.mail import EmailMultiAlternatives
from django.http import HttpResponseForbidden
from django.shortcuts import redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from .forms import AdminAccessSettingForm, OTPChallengeForm
from .middleware import OTP_VERIFIED_SESSION_KEY
from .models import AdminAccessSetting, AdminEmailOTP

logger = logging.getLogger(__name__)


# ── Helpers ────────────────────────────────────────────────────────────────

def _client_ip(request) -> str:
    fwd = request.META.get('HTTP_X_FORWARDED_FOR', '')
    if fwd:
        return fwd.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR', '') or ''


def _from_email() -> str:
    return (
        getattr(settings, 'DEFAULT_FROM_EMAIL', None)
        or getattr(settings, 'SERVER_EMAIL', None)
        or 'no-reply@example.com'
    )


def send_admin_otp(user, request) -> AdminEmailOTP | None:
    """
    Issue a new OTP for `user` and email it. Returns the OTP row, or
    None if the user has no email on file (in which case OTP can't work).
    """
    if not user.email:
        logger.warning(
            'Admin OTP requested for user %s but they have no email address.',
            user.pk,
        )
        return None

    setting = AdminAccessSetting.load()
    otp, raw_code = AdminEmailOTP.issue_for(
        user,
        validity_minutes=setting.otp_validity_minutes,
        ip=_client_ip(request),
        user_agent=request.META.get('HTTP_USER_AGENT', '')[:255],
    )

    ctx = {
        'user': user,
        'code': raw_code,
        'minutes': setting.otp_validity_minutes,
        'ip': otp.ip_address or '',
        'sent_at': timezone.now(),
    }
    try:
        html = render_to_string('admin_security/email/otp_code.html', ctx)
        text = (
            f'Your admin login code is: {raw_code}\n\n'
            f'It expires in {setting.otp_validity_minutes} minutes. '
            f"If you didn't request this, ignore this email and change "
            f'your password.'
        )
        msg = EmailMultiAlternatives(
            subject='Your admin login code',
            body=text,
            from_email=_from_email(),
            to=[user.email],
        )
        msg.attach_alternative(html, 'text/html')
        msg.send(fail_silently=True)
    except Exception:
        logger.exception('Failed to email admin OTP to %s', user.email)

    return otp


# ── Settings page ──────────────────────────────────────────────────────────

@never_cache
@login_required
@user_passes_test(lambda u: u.is_superuser, login_url='/admin/login/')
@require_http_methods(['GET', 'POST'])
def settings_view(request):
    """
    Superuser-only page to flip the kill-switch and the OTP requirement.
    Records `last_changed_by = request.user` on every save — that's the
    user who keeps emergency access if the switch is set to OFF.
    """
    setting = AdminAccessSetting.load()

    if request.method == 'POST':
        form = AdminAccessSettingForm(request.POST, instance=setting)
        if form.is_valid():
            obj = form.save(commit=False)
            obj.last_changed_by = request.user
            obj.save()
            messages.success(request, 'Admin access settings updated.')
            return redirect('admin_security:settings')
    else:
        form = AdminAccessSettingForm(instance=setting)

    return render(request, 'admin_security/settings.html', {
        'form': form,
        'setting': setting,
    })


# ── OTP challenge ──────────────────────────────────────────────────────────

@never_cache
@login_required
@require_http_methods(['GET', 'POST'])
def otp_challenge_view(request):
    """
    The screen between password-login and the actual admin. The login
    signal has already issued + emailed an OTP; we just collect and verify.

    If for some reason there's no pending OTP (e.g. the user navigated
    here directly), we issue a fresh one automatically.
    """
    user = request.user
    next_url = request.GET.get('next') or request.POST.get('next') or '/admin/'

    pending = AdminEmailOTP.objects.filter(
        user=user, consumed_at__isnull=True,
    ).order_by('-created_at').first()

    if pending is None or pending.is_expired:
        pending = send_admin_otp(user, request)

    if pending is None:
        # Means the user has no email on file. Disable OTP for them
        # silently isn't safe — instead we tell them to contact an admin.
        return render(request, 'admin_security/otp_challenge.html', {
            'form': None,
            'no_email': True,
        })

    form = OTPChallengeForm(request.POST or None)

    if request.method == 'POST' and form.is_valid():
        # Re-fetch the pending row inside the POST so we always verify
        # against the latest code on file (in case "resend" was hit).
        pending = AdminEmailOTP.objects.filter(
            user=user, consumed_at__isnull=True,
        ).order_by('-created_at').first()

        if pending and pending.verify(form.cleaned_data['code']):
            request.session[OTP_VERIFIED_SESSION_KEY] = user.pk
            request.session.modified = True
            messages.success(request, 'Verified — welcome.')
            # Only redirect to internal paths.
            if not next_url.startswith('/'):
                next_url = '/admin/'
            return redirect(next_url)

        if pending and pending.is_locked:
            messages.error(
                request,
                'Too many incorrect attempts. Request a new code.',
            )
        elif pending and pending.is_expired:
            messages.error(request, 'That code has expired. Request a new one.')
        else:
            messages.error(request, 'Incorrect code.')

    return render(request, 'admin_security/otp_challenge.html', {
        'form': form,
        'next': next_url,
        'sent_to': _mask_email(user.email),
        'no_email': False,
    })


@never_cache
@login_required
@require_http_methods(['POST'])
def resend_otp_view(request):
    """Issue a new OTP for the current user. Light rate-limit via session."""
    last = request.session.get('_admin_otp_last_sent')
    now = timezone.now().timestamp()
    if last and now - last < 30:
        messages.warning(
            request, 'Please wait a few seconds before requesting another code.',
        )
    else:
        if send_admin_otp(request.user, request):
            request.session['_admin_otp_last_sent'] = now
            messages.success(request, 'A new code has been emailed to you.')
        else:
            messages.error(
                request,
                'Could not send the code (no email address on file).',
            )
    next_url = request.POST.get('next') or '/admin/'
    return redirect(f"{reverse('admin_security:otp_challenge')}?next={next_url}")


def _mask_email(email: str) -> str:
    """alice@example.com → a****@example.com (for display only)."""
    if not email or '@' not in email:
        return email or ''
    local, _, domain = email.partition('@')
    if len(local) <= 1:
        return f'*@{domain}'
    return f'{local[0]}{"*" * (len(local) - 1)}@{domain}'
