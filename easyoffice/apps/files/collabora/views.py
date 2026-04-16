"""
apps/files/collabora/views.py

User-facing views:

  - CollaboraEditorView: the page a user lands on when they click "Edit".
    It renders an HTML form that POSTs to Collabora's cool.html endpoint,
    which boots the editor inside an iframe on this page.

  - PresencePingView: called every 30s from the browser while the editor
    is open, keeps CollaboraSession.last_seen fresh.

  - PresenceListView: returns JSON listing active editors for a file;
    consumed by the file card avatar stack.

  - EndSessionView: called on beforeunload/closed-tab to mark the
    session as ended so the UI updates promptly.
"""
import logging
from urllib.parse import urlencode
from django.conf import settings
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import JsonResponse, HttpResponseForbidden, HttpResponseBadRequest
from django.shortcuts import get_object_or_404
from django.views.generic import View
from django.views.decorators.csrf import csrf_exempt
from django.utils.decorators import method_decorator
from django.utils import timezone
from django.template.response import TemplateResponse

from apps.files.models import SharedFile
# Re-use the existing permission helper — do NOT duplicate the logic.
from apps.files.views import _file_permission_for

from .models import CollaboraSession
from .tokens import issue_token


logger = logging.getLogger(__name__)


# File types we allow the editor to open.
EDITABLE_EXTS = {'docx', 'xlsx', 'pptx'}


def _collabora_server():
    """The Collabora Online base URL (e.g. https://cool.example.org)."""
    url = getattr(settings, 'COLLABORA_SERVER_URL', '').rstrip('/')
    if not url:
        raise RuntimeError(
            'COLLABORA_SERVER_URL is not configured in settings.'
        )
    return url


def _wopi_src_for(request, file):
    """
    The absolute URL Collabora will call back to fetch the file.
    Must be reachable from the Collabora container.
    """
    # Build scheme+host from the request, then append the WOPI path.
    host = getattr(settings, 'COLLABORA_WOPI_HOST', None)
    if host:
        base = host.rstrip('/')
    else:
        base = request.build_absolute_uri('/')[:-1]
    return f'{base}/files/collabora/wopi/files/{file.pk}'


def _editor_url(request, file, lang='en'):
    """
    Build the Collabora `cool.html` URL (without the access_token which
    is submitted as a form field by the launch page).

    We ask Collabora which URL to use by hitting its discovery endpoint —
    but for simplicity we use the conventional cool.html path. If your
    Collabora deployment uses a different entry path, set
    COLLABORA_EDITOR_PATH in settings.
    """
    ext = file.extension
    server = _collabora_server()
    path = getattr(settings, 'COLLABORA_EDITOR_PATH', '/browser/dist/cool.html')

    params = {
        'WOPISrc': _wopi_src_for(request, file),
        'lang':    lang,
        # 'closebutton': 'true',   # show Collabora's own close button
        # 'revisionhistory': 'true',
    }
    return f'{server}{path}?{urlencode(params)}'


# ── Editor launch ────────────────────────────────────────────────────────────

class CollaboraEditorView(LoginRequiredMixin, View):
    """
    GET /files/<uuid>/edit/

    Renders the editor host page. Issues a short-lived token, creates a
    CollaboraSession row, and renders a form that POSTs access_token to
    Collabora.
    """
    template_name = 'files/collabora/editor.html'

    def get(self, request, pk):
        file = get_object_or_404(SharedFile, pk=pk)

        # Extension check — don't open things Collabora can't edit.
        if file.extension not in EDITABLE_EXTS:
            return HttpResponseBadRequest(
                f'Files of type .{file.extension} cannot be opened in the collaborative editor.'
            )

        # Permission check — view/edit/full map to ReadOnly vs. writable.
        permission = _file_permission_for(request.user, file)
        if permission is None:
            return HttpResponseForbidden('You do not have access to this file.')

        # Create the presence session BEFORE issuing the token so we can
        # embed session_id in the token and correlate WOPI calls with it.
        session = CollaboraSession.objects.create(
            file=file,
            user=request.user,
            permission=permission,
        )

        token = issue_token(request.user, file, permission, session.pk)
        editor_url = _editor_url(request, file)

        ctx = {
            'file':        file,
            'session':     session,
            'editor_url':  editor_url,
            'access_token': token,
            'token_ttl_ms': getattr(settings, 'COLLABORA_TOKEN_TTL_SECONDS', 36000) * 1000,
            'permission':  permission,
        }
        return TemplateResponse(request, self.template_name, ctx)


# ── Presence API ─────────────────────────────────────────────────────────────

@method_decorator(csrf_exempt, name='dispatch')
class PresencePingView(LoginRequiredMixin, View):
    """
    POST /files/collabora/presence/ping/
    Body: { "session_id": "<uuid>" }

    Called every 30s by the editor page. Updates last_seen + broadcasts
    the refreshed presence list via Channels.
    """

    def post(self, request):
        import json
        try:
            body = json.loads(request.body or b'{}')
        except ValueError:
            return HttpResponseBadRequest('invalid json')

        sid = body.get('session_id')
        if not sid:
            return HttpResponseBadRequest('session_id required')

        try:
            session = CollaboraSession.objects.select_related('file').get(
                pk=sid, user=request.user
            )
        except CollaboraSession.DoesNotExist:
            return HttpResponseBadRequest('unknown session')

        if session.ended_at:
            return JsonResponse({'ok': True, 'ended': True})

        session.touch()
        _broadcast_presence(session.file_id)
        return JsonResponse({'ok': True})


@method_decorator(csrf_exempt, name='dispatch')
class EndSessionView(LoginRequiredMixin, View):
    """
    POST /files/collabora/presence/end/
    Body: { "session_id": "<uuid>" }

    Called on beforeunload so presence updates immediately when a user
    closes the editor tab.
    """

    def post(self, request):
        import json
        try:
            body = json.loads(request.body or b'{}')
        except ValueError:
            return HttpResponseBadRequest('invalid json')

        sid = body.get('session_id')
        if not sid:
            return HttpResponseBadRequest('session_id required')

        try:
            session = CollaboraSession.objects.select_related('file').get(
                pk=sid, user=request.user
            )
        except CollaboraSession.DoesNotExist:
            return JsonResponse({'ok': True})  # idempotent

        session.end()
        _broadcast_presence(session.file_id)
        return JsonResponse({'ok': True})


class PresenceListView(LoginRequiredMixin, View):
    """
    GET /files/<uuid>/presence/

    Returns JSON { "editors": [{ "user_id", "name", "avatar_url",
    "permission", "since" }, ...] } for use in the file card UI.
    """

    def get(self, request, pk):
        file = get_object_or_404(SharedFile, pk=pk)
        if _file_permission_for(request.user, file) is None:
            return HttpResponseForbidden('no access')

        editors = []
        for s in CollaboraSession.active_for_file(file):
            editors.append({
                'user_id':    s.user_id,
                'name':       getattr(s.user, 'full_name', '') or s.user.get_username(),
                'initials':   _initials(s.user),
                'permission': s.permission,
                'since':      s.started_at.isoformat(),
            })
        return JsonResponse({'editors': editors})


def _initials(user):
    name = (getattr(user, 'full_name', '') or user.get_username() or '?').strip()
    parts = [p for p in name.split() if p]
    if not parts:
        return '?'
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][:1] + parts[-1][:1]).upper()


def _broadcast_presence(file_id):
    """
    Push the updated presence list to everyone watching this file over
    WebSocket. Safe no-op if Channels isn't configured.
    """
    try:
        from channels.layers import get_channel_layer
        from asgiref.sync import async_to_sync
    except ImportError:
        return

    layer = get_channel_layer()
    if layer is None:
        return

    group = f'collabora_file_{file_id}'
    try:
        async_to_sync(layer.group_send)(
            group,
            {'type': 'presence.update', 'file_id': str(file_id)},
        )
    except Exception as exc:
        logger.warning('Presence broadcast failed: %s', exc)
