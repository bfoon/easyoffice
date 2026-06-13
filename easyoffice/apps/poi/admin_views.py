"""
apps/poi/admin_views.py
───────────────────────
Staff-side (admin / superuser) management of POIs.

  POICreateView    POST  /poi/manage/create/
        Create a User + POIProfile, issue an invite token, email the
        registration link. Idempotent-ish: re-posting an existing email
        re-issues an invite instead of erroring.
  POIListView      GET   /poi/manage/
        Table of all POIs + status.
  POIDisableView   POST  /poi/manage/<uuid:pk>/disable/
        Disable a POI (untrust device + deactivate account).
  POIReinviteView  POST  /poi/manage/<uuid:pk>/reinvite/
        Re-send a registration link (e.g. invite expired).
"""

import logging

from django.contrib import messages
from django.core.mail import send_mail
from django.conf import settings
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.generic import View

from apps.poi.guards import AdminPOIManageMixin
from apps.poi.integrations import get_user_model
from apps.poi.models import POIInvite, POIProfile

log = logging.getLogger(__name__)


def _is_ajax(request):
    return request.headers.get("X-Requested-With") == "XMLHttpRequest"


def _send_invite_email(request, invite):
    url = request.build_absolute_uri(
        reverse("poi_register", kwargs={"token": invite.token})
    )
    user = invite.profile.user
    try:
        send_mail(
            subject="You've been invited — complete your registration",
            message=(
                f"Hello {user.get_full_name() or user.email},\n\n"
                f"You have been granted limited portal access. Complete your "
                f"registration and set your password here:\n\n{url}\n\n"
                f"This link expires in 72 hours."
            ),
            from_email=getattr(settings, "DEFAULT_FROM_EMAIL", None),
            recipient_list=[user.email],
            fail_silently=True,
        )
    except Exception:
        log.exception("POI: failed to send invite email")
    return url


def _ensure_workspace_folder(profile):
    """Create the POI's private output folder if missing."""
    if profile.workspace_folder_id:
        return profile.workspace_folder
    from apps.files.models import FileFolder

    folder = FileFolder.objects.create(
        name=f"POI Workspace — {profile.user.get_full_name() or profile.user.email}",
        owner=profile.user,
        parent=None,
        visibility="private",
        color="#6366f1",
    )
    profile.workspace_folder = folder
    profile.save(update_fields=["workspace_folder", "updated_at"])
    return folder


class POIListView(AdminPOIManageMixin, View):
    template_name = "poi/manage_list.html"

    def get(self, request):
        pois = POIProfile.objects.select_related("user", "created_by").all()
        return render(request, self.template_name, {"pois": pois})


class POICreateView(AdminPOIManageMixin, View):
    template_name = "poi/manage_create.html"

    def get(self, request):
        return render(request, self.template_name)

    def post(self, request):
        User = get_user_model()
        email = (request.POST.get("email") or "").strip().lower()
        first = (request.POST.get("first_name") or "").strip()
        last = (request.POST.get("last_name") or "").strip()
        notes = (request.POST.get("notes") or "").strip()

        if not email:
            return self._fail(request, "Email is required.")

        existing = User.objects.filter(email__iexact=email).first()
        if existing and not getattr(existing, "poi_profile", None):
            return self._fail(
                request,
                "That email already belongs to a non-POI user.",
            )

        if existing:
            user = existing
            profile = user.poi_profile
        else:
            # Inactive until they complete registration.
            user = User.objects.create(
                email=email,
                is_active=False,
            )
            # username may be required on your User; set a safe default.
            for fname, val in (("username", email), ("first_name", first), ("last_name", last)):
                if hasattr(user, fname) and not getattr(user, fname, None):
                    setattr(user, fname, val)
            user.set_unusable_password()
            user.save()
            profile = POIProfile.objects.create(
                user=user,
                created_by=request.user,
                notes=notes,
                status=POIProfile.Status.INVITED,
            )

        _ensure_workspace_folder(profile)
        invite = POIInvite.issue(profile)
        url = _send_invite_email(request, invite)

        if _is_ajax(request):
            return JsonResponse({"ok": True, "invite_url": url, "poi_id": str(profile.id)})
        messages.success(request, f"POI invited. Registration link sent to {email}.")
        return redirect("poi_manage_list")

    def _fail(self, request, msg):
        if _is_ajax(request):
            return JsonResponse({"ok": False, "error": msg}, status=400)
        messages.error(request, msg)
        return redirect("poi_manage_create")


class POIDisableView(AdminPOIManageMixin, View):
    def post(self, request, pk):
        profile = get_object_or_404(POIProfile, pk=pk)
        reason = (request.POST.get("reason") or "").strip()
        profile.disable(by=request.user, reason=reason)
        if _is_ajax(request):
            return JsonResponse({"ok": True})
        messages.success(request, "POI disabled.")
        return redirect("poi_manage_list")


class POIReinviteView(AdminPOIManageMixin, View):
    def post(self, request, pk):
        profile = get_object_or_404(POIProfile, pk=pk)
        if profile.status == POIProfile.Status.DISABLED:
            # Re-enabling: flip back to invited so they can re-register.
            profile.status = POIProfile.Status.INVITED
            profile.save(update_fields=["status", "updated_at"])
        _ensure_workspace_folder(profile)
        invite = POIInvite.issue(profile)
        url = _send_invite_email(request, invite)
        if _is_ajax(request):
            return JsonResponse({"ok": True, "invite_url": url})
        messages.success(request, "New registration link sent.")
        return redirect("poi_manage_list")
