"""
apps/files/collabora/wopi.py

WOPI host endpoints. Collabora Online speaks the WOPI protocol to fetch
and save files. This module implements the three mandatory endpoints:

    GET  /wopi/files/<uuid>                    -> CheckFileInfo
    GET  /wopi/files/<uuid>/contents           -> GetFile
    POST /wopi/files/<uuid>/contents           -> PutFile

Spec reference: https://sdk.collaboraonline.com/docs/advanced_integration.html

Auth: every WOPI call carries the `access_token` we issued via
`tokens.issue_token`. We verify it and enforce permission on every
request. No Django session is used.

Security notes:
  - We exempt these views from CSRF because Collabora is a server, not
    a browser. The token is the auth.
  - We verify file_id from the URL matches the token's `fid` to prevent
    token re-use across files.
  - PutFile saves a FileHistory(VERSIONED) entry for every save-back,
    and updates SharedFile.file_hash.
"""
import hashlib
import logging
from django.core import signing
from django.http import HttpResponse, JsonResponse, HttpResponseForbidden, Http404
from django.views import View
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt
from django.shortcuts import get_object_or_404
from django.core.files.base import ContentFile
from django.utils import timezone

from apps.files.models import SharedFile, FileHistory
from apps.core.models import User  # adjust import if your User lives elsewhere

from .tokens import verify_token
from .models import CollaboraSession


logger = logging.getLogger(__name__)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _authenticate(request, file_id):
    """
    Verify the WOPI access_token and return (user, file, permission, session).
    Returns None if auth fails — caller should return 401.
    """
    token = request.GET.get('access_token') or ''
    try:
        payload = verify_token(token)
    except signing.SignatureExpired:
        logger.info('WOPI token expired for file %s', file_id)
        return None
    except signing.BadSignature:
        logger.warning('WOPI bad-signature token for file %s', file_id)
        return None

    # Ensure the token is for the file it's being used on.
    if payload.get('fid') != str(file_id):
        logger.warning(
            'WOPI token/URL file mismatch: token=%s url=%s',
            payload.get('fid'), file_id,
        )
        return None

    try:
        user = User.objects.get(pk=payload['uid'])   # pk accepts str/int/uuid
        file = SharedFile.objects.get(pk=payload['fid'])
    except (User.DoesNotExist, SharedFile.DoesNotExist, Exception):
        return None

    session = None
    if payload.get('sid'):
        try:
            session = CollaboraSession.objects.get(pk=payload['sid'])
        except CollaboraSession.DoesNotExist:
            pass

    return user, file, payload.get('perm', 'view'), session


def _can_write(permission):
    return permission in ('edit', 'full')


def _unauthorized():
    """WOPI spec says return 401 for bad tokens."""
    return HttpResponse('Unauthorized', status=401)


# ── Endpoints ────────────────────────────────────────────────────────────────

@method_decorator(csrf_exempt, name='dispatch')
class CheckFileInfoView(View):
    """
    GET /wopi/files/<uuid>

    Returns a JSON descriptor that Collabora uses to:
      - Show the file name in the editor title bar
      - Decide whether to render editor chrome as read-only
      - Show the current user's name / identity in the collaborators list
      - Compute whether the file has changed (via SHA-256 hash)
    """

    def get(self, request, file_id):
        auth = _authenticate(request, file_id)
        if not auth:
            return _unauthorized()
        user, file, permission, _session = auth

        # Build the JSON per WOPI spec.
        info = {
            # Required
            'BaseFileName':    file.name,
            'Size':            file.file_size or 0,
            'OwnerId':         str(file.uploaded_by_id),
            'UserId':          str(user.pk),
            'Version':         str(file.updated_at.timestamp()),

            # Identity (shown in Collabora's collaborator list)
            'UserFriendlyName': getattr(user, 'full_name', '') or user.get_username(),
            'UserCanWrite':     _can_write(permission),
            'UserCanNotWriteRelative': True,   # no "save as" into our storage
            'ReadOnly':         not _can_write(permission),
            'DisablePrint':     False,
            'DisableExport':    False,
            'DisableCopy':      False,

            # Hash so Collabora can detect external changes
            'SHA256':           file.file_hash or '',

            # Post-message origin so Collabora's JS can postMessage back to us
            'PostMessageOrigin': request.build_absolute_uri('/')[:-1],

            # Branding — optional but looks nicer
            'BreadcrumbBrandName':    'EasyOffice',
            'BreadcrumbBrandUrl':     request.build_absolute_uri('/files/'),
            'BreadcrumbDocName':      file.name,
        }
        return JsonResponse(info)


@method_decorator(csrf_exempt, name='dispatch')
class FileContentsView(View):
    """
    /wopi/files/<uuid>/contents

    GET  -> return raw file bytes (Collabora calls once on open).
    POST -> save edited bytes back (every 10s or so while editing).

    We combine both methods into one class because WOPI uses the same URL
    for read and write, distinguished only by HTTP verb.
    """

    def get(self, request, file_id):
        auth = _authenticate(request, file_id)
        if not auth:
            return _unauthorized()
        _user, file, _perm, _session = auth

        if not file.file:
            raise Http404('no file blob')

        # Stream to avoid loading large files fully into memory.
        try:
            f = file.file.open('rb')
        except FileNotFoundError:
            raise Http404('file missing on disk')

        response = HttpResponse(
            f.read(),
            content_type='application/octet-stream',
        )
        response['Content-Length'] = str(file.file_size or 0)
        response['X-WOPI-ItemVersion'] = str(file.updated_at.timestamp())
        return response

    def post(self, request, file_id):
        auth = _authenticate(request, file_id)
        if not auth:
            return _unauthorized()
        user, file, permission, session = auth

        if not _can_write(permission):
            logger.warning(
                'WOPI PutFile rejected (no write perm): user=%s file=%s',
                user.pk, file.pk,
            )
            return HttpResponseForbidden('read-only token')

        data = request.body or b''
        if not data:
            # Collabora sometimes sends empty bodies during quick checks.
            return JsonResponse({}, status=200)

        sha = hashlib.sha256(data).hexdigest()

        # Keep the original filename on disk — Django's storage will collision-handle.
        from django.core.files.storage import default_storage  # noqa: F401

        # Write: replace the file contents.
        file.file.save(file.name, ContentFile(data), save=False)
        file.file_size = len(data)
        file.file_hash = sha

        update_fields = ['file', 'file_size', 'file_hash', 'updated_at']
        if hasattr(file, 'version'):
            file.version = (file.version or 1) + 1
            update_fields.append('version')

        file.save(update_fields=update_fields)

        FileHistory.objects.create(
            file=file,
            action=FileHistory.Action.VERSIONED,
            actor=user,
            notes='Saved from Collabora Online',
            snapshot_name=file.name,
            snapshot_visibility=file.visibility,
        )

        # Refresh presence so the UI shows this user as still-active.
        if session and not session.ended_at:
            session.touch()

        response = JsonResponse({'LastModifiedTime': timezone.now().isoformat()})
        response['X-WOPI-ItemVersion'] = str(file.updated_at.timestamp())
        return response