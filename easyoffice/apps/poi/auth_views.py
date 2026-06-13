"""
apps/poi/auth_views.py
──────────────────────
The POI authentication flow:

  1. POIRegisterView   GET/POST  /poi/register/<token>/
        - validates the invite token
        - POI sets their password (first time) + captures device fingerprint
        - issues an OTP and moves to verify step
  2. POIOtpView        GET/POST  /poi/register/<token>/otp/
        - verifies OTP → activates profile (trusted 30 days) + binds device
        - logs the POI in and redirects to the portal
  3. POILoginView      GET/POST  /poi/login/
        - returning POI: password check
        - if device already trusted + within window → straight in
        - else issue OTP → POILoginOtpView
  4. POILoginOtpView   GET/POST  /poi/login/otp/
        - verify OTP → re-bind device, refresh nothing (window unchanged on
          ordinary login; only registration starts a fresh 30 days)

Device fingerprint: the templates compute a stable fingerprint client-side and
post it as `device_fp`; we also drop it as the `poi_device` cookie so subsequent
full-page loads pass the guard.
"""

import logging

from django.contrib import messages
from django.contrib.auth import login, logout
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.generic import View

from apps.poi.guards import DEVICE_COOKIE, device_fingerprint
from apps.poi.integrations import (
    get_user_model,
    issue_otp,
    is_device_trusted,
    trust_device,
    verify_otp,
)
from apps.poi.models import POIInvite, POIProfile, TRUST_DAYS

log = logging.getLogger(__name__)

SESSION_PENDING_USER = "poi_pending_user_id"
SESSION_PENDING_FP = "poi_pending_fp"
SESSION_PENDING_KIND = "poi_pending_kind"  # "register" | "login"


def _set_device_cookie(response, fp):
    if fp:
        response.set_cookie(
            DEVICE_COOKIE, fp,
            max_age=60 * 60 * 24 * TRUST_DAYS,
            httponly=False,   # JS must read/refresh it
            samesite="Lax",
            secure=True,
        )
    return response


class POIRegisterView(View):
    template_name = "poi/register.html"

    def get(self, request, token):
        invite = get_object_or_404(POIInvite, token=token)
        if not invite.is_valid:
            return render(request, "poi/invite_invalid.html", status=410)
        return render(request, self.template_name, {
            "invite": invite,
            "poi": invite.profile,
            "email": invite.profile.user.email,
        })

    def post(self, request, token):
        invite = get_object_or_404(POIInvite, token=token)
        if not invite.is_valid:
            return render(request, "poi/invite_invalid.html", status=410)

        profile = invite.profile
        user = profile.user

        password = (request.POST.get("password") or "").strip()
        confirm = (request.POST.get("confirm") or "").strip()
        fp = (request.POST.get("device_fp") or "").strip()

        errors = []
        if len(password) < 8:
            errors.append("Password must be at least 8 characters.")
        if password != confirm:
            errors.append("Passwords do not match.")
        if not fp:
            errors.append("Could not identify this device. Enable JavaScript and retry.")
        if errors:
            for e in errors:
                messages.error(request, e)
            return render(request, self.template_name, {
                "invite": invite, "poi": profile, "email": user.email,
            })

        user.set_password(password)
        if not user.is_active:
            user.is_active = True
        user.save(update_fields=["password", "is_active"])

        # Stash pending state, send OTP, advance to verify step.
        request.session[SESSION_PENDING_USER] = str(user.pk)
        request.session[SESSION_PENDING_FP] = fp
        request.session[SESSION_PENDING_KIND] = "register"
        issue_otp(user, request=request)

        return redirect("poi_register_otp", token=token)


class POIOtpView(View):
    template_name = "poi/otp.html"

    def get(self, request, token):
        get_object_or_404(POIInvite, token=token)
        if not request.session.get(SESSION_PENDING_USER):
            return redirect("poi_register", token=token)
        return render(request, self.template_name, {"mode": "register", "token": token})

    def post(self, request, token):
        invite = get_object_or_404(POIInvite, token=token)
        User = get_user_model()

        user_id = request.session.get(SESSION_PENDING_USER)
        fp = request.session.get(SESSION_PENDING_FP, "")
        if not user_id:
            return redirect("poi_register", token=token)

        user = get_object_or_404(User, pk=user_id)
        code = (request.POST.get("code") or "").strip()

        if not verify_otp(user, code, request=request):
            messages.error(request, "Incorrect or expired code.")
            return render(request, self.template_name, {"mode": "register", "token": token})

        profile = user.poi_profile
        # Fresh 30-day window starts now; bind device in the shared device table.
        profile.activate(fingerprint=fp, days=TRUST_DAYS)
        trust_device(user, fp, trusted_until=profile.trusted_until, request=request)

        if invite.is_valid:
            invite.mark_used()

        for k in (SESSION_PENDING_USER, SESSION_PENDING_FP, SESSION_PENDING_KIND):
            request.session.pop(k, None)

        login(request, user)
        resp = redirect("poi_portal")
        return _set_device_cookie(resp, fp)


class POILoginView(View):
    template_name = "poi/login.html"

    def get(self, request):
        return render(request, self.template_name)

    def post(self, request):
        User = get_user_model()
        email = (request.POST.get("email") or "").strip().lower()
        password = (request.POST.get("password") or "").strip()
        fp = (request.POST.get("device_fp") or "").strip()

        user = User.objects.filter(email__iexact=email).first()
        profile = getattr(user, "poi_profile", None) if user else None

        # Uniform failure message — don't reveal whether the email is a POI.
        if not user or profile is None or not user.check_password(password):
            messages.error(request, "Invalid credentials.")
            return render(request, self.template_name)

        if profile.status == POIProfile.Status.DISABLED:
            messages.error(request, "This account is disabled.")
            return render(request, self.template_name)
        if profile.is_expired or profile.status == POIProfile.Status.EXPIRED:
            messages.error(request, "Your access window has ended. Contact an administrator.")
            return render(request, self.template_name)

        # Trusted device + valid window → straight in.
        if fp and profile.device_matches(fp) and is_device_trusted(user, fp) and profile.is_access_valid:
            login(request, user)
            resp = redirect("poi_portal")
            return _set_device_cookie(resp, fp)

        # New/unrecognised device → OTP step.
        request.session[SESSION_PENDING_USER] = str(user.pk)
        request.session[SESSION_PENDING_FP] = fp
        request.session[SESSION_PENDING_KIND] = "login"
        issue_otp(user, request=request)
        return redirect("poi_login_otp")


class POILoginOtpView(View):
    template_name = "poi/otp.html"

    def get(self, request):
        if not request.session.get(SESSION_PENDING_USER):
            return redirect("poi_login")
        return render(request, self.template_name, {"mode": "login"})

    def post(self, request):
        User = get_user_model()
        user_id = request.session.get(SESSION_PENDING_USER)
        fp = request.session.get(SESSION_PENDING_FP, "")
        if not user_id:
            return redirect("poi_login")

        user = get_object_or_404(User, pk=user_id)
        code = (request.POST.get("code") or "").strip()
        if not verify_otp(user, code, request=request):
            messages.error(request, "Incorrect or expired code.")
            return render(request, self.template_name, {"mode": "login"})

        profile = user.poi_profile
        if not profile.is_access_valid and not profile.is_expired:
            # e.g. was INVITED but somehow here — treat as activation.
            profile.activate(fingerprint=fp, days=TRUST_DAYS)
        else:
            # Ordinary re-verification on a new device: bind device, keep window.
            profile.device_fingerprint = fp
            profile.save(update_fields=["device_fingerprint", "updated_at"])
        trust_device(user, fp, trusted_until=profile.trusted_until, request=request)

        for k in (SESSION_PENDING_USER, SESSION_PENDING_FP, SESSION_PENDING_KIND):
            request.session.pop(k, None)

        login(request, user)
        resp = redirect("poi_portal")
        return _set_device_cookie(resp, fp)


class POILogoutView(View):
    def get(self, request):
        logout(request)
        return redirect("poi_login")

    post = get
