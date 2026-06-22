"""
apps/mobile_api/views_files_sign.py
───────────────────────────────────
Mobile endpoints for:
  1. Browsing & downloading Files (apps.files.SharedFile)
  2. The full signature-signing flow (apps.files.SignatureRequest*)

All endpoints are JWT-authenticated (DRF default for the mobile_api app).

The signing flow deliberately mirrors the web flow in apps/files/views.py:
    list my pending requests  →  request detail (+ my positioned fields)
    →  fill each field         →  final sign  →  finalise_signature_after_sign

The signer is identified server-side by request.user, then matched to the
SignatureRequestSigner row whose `user` is request.user. We never trust a
signer id from the client — we resolve it from the authenticated user.
"""

import logging

from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.utils import timezone

from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.models import User
from apps.files.models import (
    SharedFile, FileFolder, FileShareAccess,
    SignatureRequest, SignatureRequestSigner, SignatureField,
    SavedSignature,
)

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Shared helpers
# ─────────────────────────────────────────────────────────────────────────────

def _user_units(user):
    profile = getattr(user, 'staffprofile', None)
    unit = getattr(profile, 'unit', None) if profile else None
    dept = getattr(profile, 'department', None) if profile else None
    return unit, dept


def _accessible_files_qs(user):
    """
    Files this user is allowed to see. Mirrors the visibility rules used on
    the web (SharedFile.visibility + explicit shares + share_access).
    Only latest versions.
    """
    unit, dept = _user_units(user)
    return (
        SharedFile.objects.filter(is_latest=True)
        .filter(
            Q(uploaded_by=user) |
            Q(visibility='office') |
            Q(visibility='unit', unit=unit) |
            Q(visibility='department', department=dept) |
            Q(shared_with=user) |
            Q(share_access__user=user)
        )
        .distinct()
    )


def _serialize_file(f, request):
    try:
        url = request.build_absolute_uri(f.file.url)
    except Exception:
        url = ''
    return {
        'id': str(f.id),
        'name': f.name,
        'url': url,
        'size': f.file_size,
        'size_display': f.size_display,
        'extension': f.extension,
        'is_image': f.is_image,
        'is_pdf': f.is_pdf,
        'type_category': f.type_category,
        'icon_class': f.icon_class,
        'icon_color': f.icon_color,
        'folder_id': str(f.folder_id) if f.folder_id else None,
        'folder_name': f.folder.name if f.folder_id and f.folder else '',
        'uploaded_by': _safe_name(f.uploaded_by),
        'created_at': f.created_at.isoformat(),
    }


def _safe_name(user):
    if not user:
        return ''
    try:
        return user.full_name or user.get_full_name() or user.username
    except Exception:
        return str(user)


# ─────────────────────────────────────────────────────────────────────────────
# FILES — browse & search
# ─────────────────────────────────────────────────────────────────────────────

class MobileFileListView(APIView):
    """
    GET /api/mobile/v1/files/
        ?q=<search>          optional name search
        ?folder=<uuid>       optional folder filter
        ?category=document|image|spreadsheet|presentation|pdf  optional
        ?limit=50

    Returns files the user can access (latest versions only).
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        q = (request.query_params.get('q') or '').strip()
        folder = (request.query_params.get('folder') or '').strip()
        category = (request.query_params.get('category') or '').strip().lower()
        try:
            limit = max(1, min(200, int(request.query_params.get('limit') or 50)))
        except ValueError:
            limit = 50

        qs = _accessible_files_qs(request.user).select_related('folder', 'uploaded_by')

        if q:
            qs = qs.filter(name__icontains=q)
        if folder:
            qs = qs.filter(folder_id=folder)

        qs = qs.order_by('-created_at')[:limit]

        files = list(qs)

        if category:
            if category == 'pdf':
                files = [f for f in files if f.is_pdf]
            else:
                files = [f for f in files if f.type_category == category]

        return Response({
            'files': [_serialize_file(f, request) for f in files],
            'count': len(files),
        })


class MobileFileDetailView(APIView):
    """GET /api/mobile/v1/files/<file_id>/ — single file with a fresh URL."""
    permission_classes = [IsAuthenticated]

    def get(self, request, file_id):
        f = get_object_or_404(_accessible_files_qs(request.user), id=file_id)
        return Response(_serialize_file(f, request))


# ─────────────────────────────────────────────────────────────────────────────
# SIGNATURES — list my requests
# ─────────────────────────────────────────────────────────────────────────────

def _my_signer_rows(user, only_open=True):
    """
    SignatureRequestSigner rows that belong to this user.
    only_open → exclude already-signed/declined and dead requests.
    """
    qs = (
        SignatureRequestSigner.objects
        .filter(user=user)
        .select_related('request', 'request__document', 'request__created_by')
    )
    if only_open:
        qs = qs.exclude(status__in=['signed', 'declined', 'cancelled'])
        qs = qs.exclude(request__status__in=['cancelled', 'expired', 'completed'])
    return qs.order_by('-request__created_at')


def _serialize_signer_request(signer, request):
    sig_req = signer.request
    return {
        'request_id': str(sig_req.id),
        'signer_token': str(signer.token),
        'title': sig_req.title,
        'message': sig_req.message or '',
        'document_name': sig_req.document.name if sig_req.document_id else '',
        'status': sig_req.status,
        'signer_status': signer.status,
        'ordered_signing': bool(sig_req.ordered_signing),
        'my_order': signer.order,
        'created_by': _safe_name(sig_req.created_by),
        'created_at': sig_req.created_at.isoformat(),
        'expires_at': sig_req.expires_at.isoformat() if sig_req.expires_at else None,
        # Token-based preview URL — no login needed, the token authorises it.
        'preview_url': request.build_absolute_uri(
            f'/files/sign/{signer.token}/preview/'
        ),
        'download_url': request.build_absolute_uri(
            f'/files/sign/{signer.token}/download/'
        ),
    }


class MobileSignRequestsView(APIView):
    """
    GET /api/mobile/v1/sign/requests/?status=open|all
    Lists signature requests where the current user is a signer.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        which = (request.query_params.get('status') or 'open').strip().lower()
        only_open = which != 'all'
        rows = _my_signer_rows(request.user, only_open=only_open)
        return Response({
            'requests': [_serialize_signer_request(s, request) for s in rows],
            'count': rows.count(),
        })


class MobileSignRequestDetailView(APIView):
    """
    GET /api/mobile/v1/sign/requests/<request_id>/
    Returns the request, my positioned fields, and my saved signatures.

    The client renders the PDF (from preview_url) and overlays the field
    rectangles using the percentage coordinates, exactly like the web page.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, request_id):
        signer = get_object_or_404(
            SignatureRequestSigner.objects.select_related(
                'request', 'request__document', 'request__created_by'
            ),
            request_id=request_id,
            user=request.user,
        )
        sig_req = signer.request

        if sig_req.status in ('cancelled', 'expired'):
            return Response({'error': 'This signing request is no longer available.'},
                            status=403)

        # Ordered signing guard — surface, don't block the read.
        blocked = False
        if sig_req.ordered_signing and signer.order > 1:
            blocked = sig_req.signers.filter(
                order__lt=signer.order
            ).exclude(status='signed').exists()

        # Mark viewed (same side effect as the web GET).
        changed = []
        if signer.status == 'pending':
            signer.status = 'viewed'
            changed.append('status')
        if not signer.viewed_at:
            signer.viewed_at = timezone.now()
            changed.append('viewed_at')
        if changed:
            signer.save(update_fields=changed)

        fields = (
            SignatureField.objects.filter(request=sig_req, signer=signer)
            .order_by('page', 'y_pct', 'x_pct')
        )

        saved = (
            SavedSignature.objects.filter(user=request.user)
            .order_by('-is_default', '-created_at')[:12]
        )
        saved_out = []
        for s in saved:
            saved_out.append({
                'id': str(s.id),
                'name': s.name,
                'sig_type': s.sig_type,
                'is_default': s.is_default,
                # For typed: the text. For drawn: base64 data URI in `data`.
                'data': s.data or '',
            })

        return Response({
            'request': _serialize_signer_request(signer, request),
            'blocked_by_order': blocked,
            'fields': [f.to_dict() for f in fields],
            'saved_signatures': saved_out,
        })


# ─────────────────────────────────────────────────────────────────────────────
# SIGNATURES — fill a field, then submit
# ─────────────────────────────────────────────────────────────────────────────

class MobileSignFieldView(APIView):
    """
    POST /api/mobile/v1/sign/requests/<request_id>/fields/<field_id>/
    Body: { "value": "<text | base64 data URI>" }

    Saves one field value. Mirrors web FillSignatureFieldView.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, request_id, field_id):
        signer = get_object_or_404(
            SignatureRequestSigner.objects.select_related('request'),
            request_id=request_id,
            user=request.user,
        )
        sig_req = signer.request

        if sig_req.status in ('cancelled', 'expired'):
            return Response({'error': 'This request is no longer available.'}, status=403)

        if sig_req.ordered_signing and signer.order > 1:
            prev_pending = sig_req.signers.filter(
                order__lt=signer.order
            ).exclude(status='signed').exists()
            if prev_pending:
                return Response(
                    {'error': 'A previous signer must sign before you.'}, status=403
                )

        field = get_object_or_404(
            SignatureField, id=field_id, request=sig_req, signer=signer
        )

        value = str(request.data.get('value') or '').strip()
        if not value:
            return Response({'error': 'Please complete this field.'}, status=400)

        field.value = value
        if hasattr(field, 'filled_at'):
            field.filled_at = timezone.now()
            field.save(update_fields=['value', 'filled_at'])
        else:
            field.save(update_fields=['value'])

        total = SignatureField.objects.filter(request=sig_req, signer=signer).count()
        filled = SignatureField.objects.filter(
            request=sig_req, signer=signer
        ).exclude(value='').count()

        return Response({
            'ok': True,
            'field_id': str(field.id),
            'filled_fields': filled,
            'total_fields': total,
            'all_fields_done': total == filled,
        })


class MobileSignSubmitView(APIView):
    """
    POST /api/mobile/v1/sign/requests/<request_id>/submit/
    Body: {
        "signature_data": "<base64 data URI | typed text>",
        "signature_type": "draw|type|upload",
        "save_signature": false,
        "save_signature_name": "My Signature"
    }

    Marks the signer 'signed' and dispatches the SAME background finaliser
    the web uses (finalise_signature_after_sign) which stamps the PDF and
    sends completion notifications.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, request_id):
        signer = get_object_or_404(
            SignatureRequestSigner.objects.select_related('request'),
            request_id=request_id,
            user=request.user,
        )
        sig_req = signer.request

        if signer.status == 'signed':
            return Response({'ok': True, 'already_signed': True})

        if sig_req.status in ('cancelled', 'expired'):
            return Response({'error': 'This request is no longer available.'}, status=403)

        # Ordered signing guard
        if sig_req.ordered_signing and signer.order > 1:
            prev_pending = sig_req.signers.filter(
                order__lt=signer.order
            ).exclude(status='signed').exists()
            if prev_pending:
                return Response(
                    {'error': 'A previous signer must sign before you.'}, status=403
                )

        sig_data = str(request.data.get('signature_data') or '').strip()
        sig_type = (request.data.get('signature_type') or 'draw').strip()
        if not sig_data:
            return Response({'error': 'Signature is required.'}, status=400)

        # All my required fields must be filled.
        total = SignatureField.objects.filter(request=sig_req, signer=signer).count()
        if total:
            filled = SignatureField.objects.filter(
                request=sig_req, signer=signer
            ).exclude(value='').count()
            if filled != total:
                return Response(
                    {'error': 'Please complete all required fields before signing.'},
                    status=400,
                )

        signer.status = 'signed'
        signer.signed_at = timezone.now()
        signer.signature_data = sig_data
        signer.signature_type = sig_type
        try:
            from apps.files.views import _get_client_ip
            signer.ip_address = _get_client_ip(request)
        except Exception:
            pass
        signer.user_agent = (request.META.get('HTTP_USER_AGENT', '') or '')[:500]
        signer.save()

        # Optionally save the signature for reuse.
        if request.data.get('save_signature') in (True, '1', 'true', 1):
            try:
                name = (request.data.get('save_signature_name') or 'My Signature').strip()
                SavedSignature.objects.create(
                    user=request.user, name=name or 'My Signature',
                    sig_type=sig_type, data=sig_data,
                )
            except Exception:
                pass

        # Audit + finaliser (same as web).
        try:
            from apps.files.views import _log_audit
            _log_audit(sig_req, 'signed', signer=signer, request=request,
                       notes=f'Signature type: {sig_type} (mobile)')
        except Exception:
            pass

        base_url = request.build_absolute_uri('/')
        try:
            from apps.files.tasks import finalise_signature_after_sign
            finalise_signature_after_sign.delay(
                sig_req_id=str(sig_req.pk),
                signer_id=str(signer.pk),
                base_url=base_url,
            )
        except Exception:
            # Broker down → run inline so it still completes.
            try:
                from apps.files.tasks import finalise_signature_after_sign
                finalise_signature_after_sign(
                    sig_req_id=str(sig_req.pk),
                    signer_id=str(signer.pk),
                    base_url=base_url,
                )
            except Exception:
                log.exception('mobile sign: finaliser failed for %s', sig_req.pk)

        return Response({'ok': True, 'signed': True})


class MobileSignDeclineView(APIView):
    """
    POST /api/mobile/v1/sign/requests/<request_id>/decline/
    Body: { "reason": "..." }
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, request_id):
        signer = get_object_or_404(
            SignatureRequestSigner.objects.select_related('request'),
            request_id=request_id,
            user=request.user,
        )
        sig_req = signer.request

        if signer.status in ('signed', 'declined'):
            return Response({'ok': True, 'already_done': True})

        signer.status = 'declined'
        signer.decline_reason = (request.data.get('reason') or '').strip()
        signer.user_agent = (request.META.get('HTTP_USER_AGENT', '') or '')[:500]
        try:
            from apps.files.views import _get_client_ip
            signer.ip_address = _get_client_ip(request)
        except Exception:
            pass
        signer.save(update_fields=['status', 'decline_reason', 'ip_address', 'user_agent'])

        try:
            from apps.files.views import _log_audit
            _log_audit(sig_req, 'declined', signer=signer, request=request,
                       notes=signer.decline_reason)
        except Exception:
            pass
        try:
            if hasattr(sig_req, 'update_status'):
                sig_req.update_status()
        except Exception:
            pass

        base_url = request.build_absolute_uri('/')
        try:
            from apps.files.tasks import notify_signature_declined
            notify_signature_declined.delay(
                sig_req_id=str(sig_req.pk),
                signer_id=str(signer.pk),
                base_url=base_url,
            )
        except Exception:
            pass

        return Response({'ok': True, 'declined': True})
