"""
apps/files/external_share_views.py
──────────────────────────────────
All view classes for the external (no-login) file-sharing feature.

Kept in its own module so views.py doesn't grow yet another 800 lines.
The main views.py just needs:

    from apps.files.external_share_views import (
        ExternalShareCreateView,
        ExternalShareListView,
        ExternalShareManageView,
        ExternalShareRevokeView,
        ExternalShareOpenView,
        ExternalShareDeviceDecideView,
        ExternalShareFingerprintView,
    )

URL routes are registered in apps/files/urls.py (see updated file).

Public (no-login) endpoints:
    /files/ext/<token>/                  → ExternalShareOpenView
    /files/ext/<token>/fp/                → ExternalShareFingerprintView (POST, JSON)
    /files/ext/device/<token>/<decision>/ → ExternalShareDeviceDecideView
                                            decision = 'accept' | 'decline'

Authenticated endpoints (the file's owner):
    /files/<file_pk>/external/create/    → ExternalShareCreateView (POST)
    /files/external/                     → ExternalShareListView (browse all)
    /files/external/<pk>/                → ExternalShareManageView (timeline + devices)
    /files/external/<pk>/revoke/         → ExternalShareRevokeView (POST)
"""
from __future__ import annotations

import logging

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import (
    FileResponse, Http404, HttpResponse, HttpResponseForbidden, JsonResponse,
)
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt
from django.views.generic import View, TemplateView
from datetime import timedelta

from apps.files.models import (
    SharedFile,
    ExternalFileShare,
    ExternalShareDevice,
    ExternalShareAuditEvent,
)
from apps.files.external_share_utils import (
    build_device_fingerprint,
    get_client_ip,
    parse_user_agent,
    write_audit,
)

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers (small, file-private)
# ─────────────────────────────────────────────────────────────────────────────

def _is_ajax(request):
    return request.headers.get('X-Requested-With') == 'XMLHttpRequest'


def _can_share_externally(user, file_obj):
    """
    Mirrors the existing _can_edit_file rule: only edit/full users can
    create or revoke external shares. We deliberately don't import
    _can_edit_file from views.py to keep this module decoupled (and avoid
    the circular import).
    """
    if not user.is_authenticated:
        return False
    if file_obj.uploaded_by_id == user.id:
        return True
    # Mirrors FileShareAccess permission check
    return file_obj.share_access.filter(user=user, permission__in=('edit', 'full')).exists()


def _absolute_base(request):
    """Build a clean http(s)://host base URL for emails."""
    return f'{request.scheme}://{request.get_host()}'


def _client_token_from_request(request):
    """
    Read the client-side fingerprint token. The recipient's browser sets it
    in localStorage on first open and posts it back as a cookie + header on
    subsequent visits. Cookie wins; header is the fallback used right after
    the JS POST that registers the device.
    """
    return (
        request.COOKIES.get('eo_ext_fp')
        or request.headers.get('X-EO-Fingerprint')
        or ''
    )


def _set_fingerprint_cookie(response, fingerprint):
    """Pin the fingerprint cookie for one year, httpOnly, sameSite=Lax."""
    response.set_cookie(
        'eo_ext_fp',
        fingerprint,
        max_age=60 * 60 * 24 * 365,
        httponly=True,
        samesite='Lax',
    )
    return response


# ─────────────────────────────────────────────────────────────────────────────
# 1) Owner-side: create a share
# ─────────────────────────────────────────────────────────────────────────────

class ExternalShareCreateView(LoginRequiredMixin, View):
    """
    POST /files/<file_pk>/external/create/

    Body params (form-encoded or JSON):
        recipient_email    (required)
        recipient_name     (optional, freeform)
        message            (optional, shown in invitation email)
        expires_in_hours   (default 168 = 7 days; max ~720 = 30 days)
        permission         'view' or 'download'  (default 'download')
        max_downloads      0 = unlimited (default 0)

    Returns JSON {ok, share: {id, open_url, expires_at, recipient_email}}.
    """

    MAX_HOURS = 24 * 30   # cap at 30 days
    DEFAULT_HOURS = 24 * 7

    def post(self, request, pk):
        f = get_object_or_404(SharedFile, pk=pk)
        if not _can_share_externally(request.user, f):
            return JsonResponse({'ok': False, 'error': 'Permission denied.'}, status=403)

        recipient_email = (request.POST.get('recipient_email') or '').strip().lower()
        if not recipient_email or '@' not in recipient_email or '.' not in recipient_email.split('@')[-1]:
            return JsonResponse({'ok': False, 'error': 'A valid recipient email is required.'}, status=400)

        recipient_name = (request.POST.get('recipient_name') or '').strip()[:200]
        message_body   = (request.POST.get('message') or '').strip()[:2000]

        try:
            hours = int(request.POST.get('expires_in_hours') or self.DEFAULT_HOURS)
        except (TypeError, ValueError):
            hours = self.DEFAULT_HOURS
        hours = max(1, min(hours, self.MAX_HOURS))

        try:
            max_downloads = int(request.POST.get('max_downloads') or 0)
        except (TypeError, ValueError):
            max_downloads = 0
        max_downloads = max(0, min(max_downloads, 1000))

        permission = (request.POST.get('permission') or 'download').strip().lower()
        if permission not in ('view', 'download'):
            permission = 'download'

        # Don't create a duplicate share to the same email if there's already
        # an active one — extend the existing share's expiry instead.
        email_hash = ExternalFileShare.hash_email(recipient_email)
        existing = (ExternalFileShare.objects
                    .filter(file=f, recipient_email_hash=email_hash,
                            status__in=[ExternalFileShare.Status.ACTIVE,
                                        ExternalFileShare.Status.PENDING])
                    .first())

        new_expiry = timezone.now() + timedelta(hours=hours)

        if existing and not existing.is_expired and not existing.is_revoked:
            # Extend / update existing share
            existing.expires_at    = new_expiry
            existing.message       = message_body
            existing.permission    = permission
            existing.max_downloads = max_downloads
            if recipient_name:
                existing.recipient_name = recipient_name
            existing.save()
            share = existing
            write_audit(share, 'email_sent',
                        actor=request.user,
                        notes=f'Share extended/refreshed (expiry={new_expiry.isoformat()})',
                        ip_address=get_client_ip(request))
            new_record = False
        else:
            share = ExternalFileShare.objects.create(
                file                  = f,
                recipient_email       = recipient_email,
                recipient_name        = recipient_name,
                recipient_email_hash  = email_hash,
                message               = message_body,
                permission            = permission,
                expires_at            = new_expiry,
                max_downloads         = max_downloads,
                created_by            = request.user,
            )
            write_audit(share, 'created', actor=request.user,
                        notes=f'External share created for {recipient_email}',
                        ip_address=get_client_ip(request))
            new_record = True

        # Background email send.
        try:
            from apps.files.tasks import send_external_share_invitation
            send_external_share_invitation.delay(share.pk, _absolute_base(request))
        except Exception:
            logger.exception('Could not enqueue invitation email for share %s', share.pk)

        return JsonResponse({
            'ok': True,
            'created': new_record,
            'share': {
                'id':              str(share.pk),
                'recipient_email': share.recipient_email,
                'recipient_name':  share.recipient_name,
                'permission':      share.permission,
                'permission_display': share.get_permission_display(),
                'expires_at':      share.expires_at.isoformat(),
                'max_downloads':   share.max_downloads,
                'open_url':        share.open_url,
                'status':          share.status,
                'manage_url':      reverse('external_share_manage', kwargs={'pk': share.pk}),
            },
        })


# ─────────────────────────────────────────────────────────────────────────────
# 2) Owner-side: list / manage / revoke
# ─────────────────────────────────────────────────────────────────────────────

class ExternalShareListView(LoginRequiredMixin, View):
    """
    GET  /files/external/                  → JSON list (for share modal panel)
    GET  /files/external/?file=<file_pk>   → only shares for that file

    Returns shares the current user can manage (i.e. shares they created OR
    shares on files they have edit/full access to).
    """

    def get(self, request):
        from django.db.models import Q

        qs = ExternalFileShare.objects.select_related('file', 'created_by')

        file_pk = request.GET.get('file')
        if file_pk:
            try:
                f = SharedFile.objects.get(pk=file_pk)
            except SharedFile.DoesNotExist:
                return JsonResponse({'ok': False, 'error': 'File not found.'}, status=404)
            if not _can_share_externally(request.user, f):
                return JsonResponse({'ok': False, 'error': 'Permission denied.'}, status=403)
            qs = qs.filter(file=f)
        else:
            # Only shares user can manage anywhere
            qs = qs.filter(
                Q(created_by=request.user)
                | Q(file__uploaded_by=request.user)
                | Q(file__share_access__user=request.user,
                    file__share_access__permission__in=('edit', 'full'))
            ).distinct()

        # Refresh status (cheap — handful of rows)
        for s in qs:
            s.refresh_status()

        # Bulk-save dirty rows in one transaction would be nicer; this is
        # fine for typical volumes.
        for s in qs:
            ExternalFileShare.objects.filter(pk=s.pk).update(status=s.status)

        rows = []
        for s in qs.order_by('-created_at')[:200]:
            rows.append({
                'id':                str(s.pk),
                'file_id':           str(s.file_id),
                'file_name':         s.file.name,
                'recipient_email':   s.recipient_email,
                'recipient_name':    s.recipient_name,
                'permission':        s.permission,
                'permission_display': s.get_permission_display(),
                'status':            s.status,
                'status_display':    s.get_status_display(),
                'expires_at':        s.expires_at.isoformat(),
                'is_expired':        s.is_expired,
                'is_revoked':        s.is_revoked,
                'is_active':         s.is_active,
                'download_count':    s.download_count,
                'max_downloads':     s.max_downloads,
                'created_at':        s.created_at.isoformat(),
                'created_by':        s.created_by.full_name if s.created_by_id else '—',
                'open_url':          s.open_url,
                'manage_url':        reverse('external_share_manage', kwargs={'pk': s.pk}),
                'device_count':      s.devices.count(),
                'pending_count':     s.devices.filter(status=ExternalShareDevice.Status.PENDING).count(),
            })

        return JsonResponse({'ok': True, 'shares': rows})


class ExternalShareManageView(LoginRequiredMixin, TemplateView):
    """Detail page for one external share — devices + audit timeline."""
    template_name = 'files/external_share_manage.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        share = get_object_or_404(ExternalFileShare, pk=kwargs['pk'])
        if not _can_share_externally(self.request.user, share.file):
            raise Http404
        share.refresh_status()
        ExternalFileShare.objects.filter(pk=share.pk).update(status=share.status)
        share.refresh_from_db(fields=['status'])
        ctx['share'] = share
        ctx['devices'] = share.devices.order_by('-last_seen_at')
        ctx['audit'] = share.audit_events.select_related('device', 'actor').order_by('-timestamp')[:200]
        return ctx


class ExternalShareRevokeView(LoginRequiredMixin, View):
    """POST /files/external/<pk>/revoke/ — owner cancels a share."""

    def post(self, request, pk):
        share = get_object_or_404(ExternalFileShare, pk=pk)
        if not _can_share_externally(request.user, share.file):
            return JsonResponse({'ok': False, 'error': 'Permission denied.'}, status=403)

        if share.is_revoked:
            return JsonResponse({'ok': True, 'message': 'Already revoked.', 'status': share.status})

        share.status = ExternalFileShare.Status.REVOKED
        share.revoked_at = timezone.now()
        share.revoked_by = request.user
        share.save(update_fields=['status', 'revoked_at', 'revoked_by'])

        write_audit(share, 'revoked', actor=request.user,
                    notes='Share revoked by owner',
                    ip_address=get_client_ip(request))

        if _is_ajax(request):
            return JsonResponse({'ok': True, 'message': 'Share revoked.', 'status': share.status})
        messages.success(request, f'Share to {share.recipient_email} was revoked.')
        return redirect('external_share_manage', pk=share.pk)


class ExternalShareDeviceManageView(LoginRequiredMixin, View):
    """
    POST /files/external/<pk>/devices/<device_pk>/<decision>/

    Owner-initiated accept/decline of a device from inside the manage page
    (instead of from the email link). decision ∈ {accept, decline, remove}.
    """

    def post(self, request, pk, device_pk, decision):
        share = get_object_or_404(ExternalFileShare, pk=pk)
        if not _can_share_externally(request.user, share.file):
            return JsonResponse({'ok': False, 'error': 'Permission denied.'}, status=403)

        device = get_object_or_404(ExternalShareDevice, pk=device_pk, share=share)

        if decision == 'accept':
            device.status = ExternalShareDevice.Status.ACCEPTED
            device.decided_at = timezone.now()
            device.decided_by = request.user
            device.save(update_fields=['status', 'decided_at', 'decided_by'])
            write_audit(share, 'device_accepted', device=device,
                        actor=request.user, notes='Accepted via manage page',
                        ip_address=get_client_ip(request))
            try:
                from apps.files.tasks import send_external_share_device_decision
                send_external_share_device_decision.delay(device.pk, _absolute_base(request))
            except Exception:
                logger.exception('Could not notify recipient about device decision')
            share.refresh_status()
            ExternalFileShare.objects.filter(pk=share.pk).update(status=share.status)
            return JsonResponse({'ok': True, 'status': device.status})

        if decision == 'decline':
            device.status = ExternalShareDevice.Status.DECLINED
            device.decided_at = timezone.now()
            device.decided_by = request.user
            device.decline_reason = (request.POST.get('reason') or '')[:500]
            device.save(update_fields=['status', 'decided_at', 'decided_by', 'decline_reason'])
            write_audit(share, 'device_declined', device=device,
                        actor=request.user, notes='Declined via manage page',
                        ip_address=get_client_ip(request))
            try:
                from apps.files.tasks import send_external_share_device_decision
                send_external_share_device_decision.delay(device.pk, _absolute_base(request))
            except Exception:
                logger.exception('Could not notify recipient about device decision')
            return JsonResponse({'ok': True, 'status': device.status})

        if decision == 'remove':
            device.delete()
            write_audit(share, 'device_declined', actor=request.user,
                        notes='Device record removed by owner',
                        ip_address=get_client_ip(request))
            return JsonResponse({'ok': True, 'status': 'removed'})

        return JsonResponse({'ok': False, 'error': 'Unknown decision.'}, status=400)


# ─────────────────────────────────────────────────────────────────────────────
# 3) Public (no-login) — recipient flow
# ─────────────────────────────────────────────────────────────────────────────

class ExternalShareOpenView(View):
    """
    GET  /files/ext/<token>/

    The link the recipient clicks. Resolves the share, sets the fingerprint
    cookie if missing, then either:

      • Renders a "registering this device, please wait" page that POSTs the
        client-side fingerprint to ExternalShareFingerprintView. After that
        round-trip we know which device this is, and the page redirects:
          - back here if the device is already accepted
          - to a "verification pending" page if the owner hasn't decided yet
          - to a "declined" page if the owner declined
          - to the file download/preview if accepted

    The single-page flow keeps the recipient on a clean URL so the QR
    scanner / mobile share sheet doesn't break, while still letting us
    collect the fingerprint via JS.
    """

    template_name = 'files/external_share_open.html'

    def _expired_response(self, share, request, why):
        write_audit(share, 'failed_attempt',
                    notes=why, ip_address=get_client_ip(request))
        return render(request, 'files/external_share_unavailable.html', {
            'share': share, 'reason': why,
        }, status=410)

    def get(self, request, token):
        share = get_object_or_404(ExternalFileShare.objects.select_related('file', 'created_by'),
                                  token=token)

        if share.is_revoked:
            return self._expired_response(share, request, 'revoked')
        if share.is_expired:
            # Reflect status if not already.
            if share.status != ExternalFileShare.Status.EXPIRED:
                share.status = ExternalFileShare.Status.EXPIRED
                share.save(update_fields=['status'])
                write_audit(share, 'expired', notes='Auto-marked on access')
            return self._expired_response(share, request, 'expired')

        # Try to resolve device from existing cookie.
        client_token = _client_token_from_request(request)
        ip = get_client_ip(request)
        ua = request.META.get('HTTP_USER_AGENT', '')

        device = None
        fingerprint = None
        if client_token:
            fingerprint = build_device_fingerprint(client_token, ua, ip)
            device = ExternalShareDevice.objects.filter(
                share=share, fingerprint=fingerprint,
            ).first()

        if device and device.is_accepted:
            # Direct hit — serve the file/preview.
            write_audit(share, 'opened', device=device,
                        notes='Repeat open from accepted device',
                        ip_address=ip)
            device.access_count += 1
            device.last_seen_at = timezone.now()
            device.save(update_fields=['access_count', 'last_seen_at'])
            return self._serve_file(request, share, device)

        if device and device.is_declined:
            return render(request, 'files/external_share_declined.html',
                          {'share': share, 'device': device}, status=403)

        # Either no device yet (first visit) or pending approval. Render the
        # interstitial page that:
        #   a) generates a JS fingerprint and POSTs it to /fp/  (then reloads)
        #   b) shows pending state once the device row exists
        ctx = {
            'share': share,
            'device': device,
            'fp_url': reverse('external_share_fingerprint', kwargs={'token': share.token}),
        }
        response = render(request, self.template_name, ctx)
        if fingerprint:
            _set_fingerprint_cookie(response, client_token)
        return response

    def _serve_file(self, request, share, device):
        """Stream the file to an accepted device. View-only = inline preview."""
        share.download_count += 1
        share.save(update_fields=['download_count'])

        write_audit(share, 'downloaded', device=device,
                    notes=f'Served {share.file.name} ({share.permission})',
                    ip_address=get_client_ip(request))

        f = share.file
        import mimetypes as _mt
        ctype, _ = _mt.guess_type(f.name)
        ctype = ctype or 'application/octet-stream'

        try:
            fh = f.file.open('rb')
        except Exception:
            raise Http404

        response = FileResponse(fh, content_type=ctype)
        if share.permission == 'view':
            response['Content-Disposition'] = f'inline; filename="{f.name}"'
        else:
            response['Content-Disposition'] = f'attachment; filename="{f.name}"'
        return response


@method_decorator(csrf_exempt, name='dispatch')
class ExternalShareFingerprintView(View):
    """
    POST /files/ext/<token>/fp/

    JSON body: { client_token, screen, timezone }

    Registers (or refreshes) an ExternalShareDevice for the recipient.
    Sets the fingerprint cookie, returns the device's current status so
    the page can render accordingly.

    CSRF-exempt because the recipient is anonymous and has no CSRF cookie
    on first visit — the share token in the URL is the authorisation
    bearer here. We also accept GET → 405 explicitly so probes are clear.
    """

    def post(self, request, token):
        import json as _json

        share = get_object_or_404(ExternalFileShare, token=token)

        if share.is_revoked or share.is_expired:
            return JsonResponse({'ok': False, 'error': 'unavailable'}, status=410)

        # Defensive body parse: tolerate empty body, bad JSON, BOM, and
        # form-encoded fallback. Must NEVER 500 on a hostile client.
        data = {}
        try:
            raw = request.body or b''
            text = raw.decode('utf-8', errors='replace').lstrip('\ufeff').strip()
            if text:
                data = _json.loads(text)
                if not isinstance(data, dict):
                    data = {}
        except Exception:
            data = {}

        # Form-encoded fallback (some networks strip JSON content-type)
        if not data and request.POST:
            data = {
                'client_token': request.POST.get('client_token', ''),
                'screen':       request.POST.get('screen', ''),
                'timezone':     request.POST.get('timezone', ''),
            }

        client_token = (data.get('client_token') or '').strip()[:200]
        screen       = (data.get('screen') or '').strip()[:50]
        tz_name      = (data.get('timezone') or '').strip()[:60]
        if not client_token:
            return JsonResponse({'ok': False, 'error': 'missing client_token'}, status=400)

        ip = get_client_ip(request)
        ua = request.META.get('HTTP_USER_AGENT', '')[:1000]
        fingerprint = build_device_fingerprint(client_token, ua, ip)

        ua_info = parse_user_agent(ua)

        device, created = ExternalShareDevice.objects.get_or_create(
            share=share,
            fingerprint=fingerprint,
            defaults={
                'ip_address':  ip or None,
                'user_agent':  ua,
                'browser_name': ua_info['browser'],
                'os_name':     ua_info['os'],
                'device_type': ua_info['device_type'],
            },
        )

        if not created:
            # Refresh telemetry for the existing device — IP can change, UA
            # can patch-bump, etc. Don't overwrite a final decision though.
            update_fields = []
            if ip and ip != device.ip_address:
                device.ip_address = ip
                update_fields.append('ip_address')
            if ua and not device.user_agent:
                device.user_agent = ua
                update_fields.append('user_agent')
            if ua_info['browser'] and not device.browser_name:
                device.browser_name = ua_info['browser']
                update_fields.append('browser_name')
            if ua_info['os'] and not device.os_name:
                device.os_name = ua_info['os']
                update_fields.append('os_name')
            if ua_info['device_type'] and not device.device_type:
                device.device_type = ua_info['device_type']
                update_fields.append('device_type')
            if update_fields:
                device.save(update_fields=update_fields)

        # New device → fire owner verification email
        if created:
            write_audit(share, 'opened', device=device,
                        notes=f'First visit (screen={screen}, tz={tz_name})',
                        ip_address=ip)
            try:
                from apps.files.tasks import send_external_share_device_verification
                send_external_share_device_verification.delay(
                    device.pk, _absolute_base(request)
                )
            except Exception:
                logger.exception('Could not enqueue device verification email for %s', device.pk)

        response = JsonResponse({
            'ok': True,
            'status':       device.status,
            'is_accepted':  device.is_accepted,
            'is_declined':  device.is_declined,
            'is_pending':   device.is_pending,
            'reload':       device.is_accepted,  # tell client to reload to download
        })
        _set_fingerprint_cookie(response, client_token)
        return response


class ExternalShareDeviceDecideView(View):
    """
    GET  /files/ext/device/<token>/<decision>/

    The owner clicks accept or decline from the verification email. Token
    is the device's verify_token (one-time, single-purpose). We require
    the owner to be logged in for safety; if they're not we redirect to
    login first and return them here.
    """

    ALLOWED = {'accept', 'decline'}

    def get(self, request, token, decision):
        if decision not in self.ALLOWED:
            return HttpResponseForbidden('Unknown decision.')

        device = get_object_or_404(
            ExternalShareDevice.objects.select_related('share', 'share__file', 'share__created_by'),
            verify_token=token,
        )
        share = device.share

        # Auth guard: only the share's owner (or someone with edit/full on
        # the file) can decide. Use Django's standard login redirect.
        if not request.user.is_authenticated:
            from django.contrib.auth.views import redirect_to_login
            return redirect_to_login(request.get_full_path())

        if not _can_share_externally(request.user, share.file):
            return HttpResponseForbidden('You do not have permission to decide this device.')

        if device.status != ExternalShareDevice.Status.PENDING:
            return render(request, 'files/external_share_device_already_decided.html', {
                'device': device, 'share': share,
            })

        if decision == 'accept':
            device.status = ExternalShareDevice.Status.ACCEPTED
            action_audit = 'device_accepted'
        else:
            device.status = ExternalShareDevice.Status.DECLINED
            device.decline_reason = (request.GET.get('reason') or '')[:500]
            action_audit = 'device_declined'
        device.decided_at = timezone.now()
        device.decided_by = request.user
        device.save()

        write_audit(share, action_audit, device=device, actor=request.user,
                    notes=f'Decision via email link',
                    ip_address=get_client_ip(request))

        # Recompute share status (PENDING → ACTIVE if first acceptance).
        share.refresh_status()
        ExternalFileShare.objects.filter(pk=share.pk).update(status=share.status)

        # Tell the recipient.
        try:
            from apps.files.tasks import send_external_share_device_decision
            send_external_share_device_decision.delay(device.pk, _absolute_base(request))
        except Exception:
            logger.exception('Could not enqueue recipient notification for %s', device.pk)

        return render(request, 'files/external_share_device_decided.html', {
            'device': device, 'share': share, 'decision': decision,
        })