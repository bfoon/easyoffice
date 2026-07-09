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

URL architecture (three distinct URLs — do not conflate them):

  COLLABORA_SERVER_URL  http://collabora:9980   Django → Collabora, internal.
                                                Used only to fetch discovery.
  COLLABORA_PUBLIC_URL  https://easyoffice.gm   Browser → nginx → Collabora.
                                                The iframe src host.
  COLLABORA_WOPI_HOST   http://web:8000         Collabora → Django, internal.
                                                Embedded as WOPISrc.
"""
import logging
import re
import xml.etree.ElementTree as ET
from urllib.parse import urlencode, urlsplit, urlunsplit
from urllib.request import urlopen

from django.conf import settings
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.cache import cache
from django.http import JsonResponse, HttpResponseForbidden, HttpResponseBadRequest
from django.shortcuts import get_object_or_404
from django.views.generic import View
from django.views.decorators.csrf import csrf_exempt
from django.utils.decorators import method_decorator
from django.template.response import TemplateResponse

from apps.files.models import SharedFile
# Re-use the existing permission helper — do NOT duplicate the logic.
from apps.files.views import _file_permission_for

from .models import CollaboraSession
from .tokens import issue_token


logger = logging.getLogger(__name__)


# File types we allow the editor to open.
EDITABLE_EXTS = {'docx', 'xlsx', 'pptx'}

_DISCOVERY_CACHE_KEY = 'collabora:discovery:urlsrc-map'
_DISCOVERY_CACHE_TTL = 60 * 60 * 12   # 12h; refetched automatically after


def _collabora_internal():
    """Internal Collabora base URL (Django → Collabora over docker network)."""
    url = getattr(settings, 'COLLABORA_SERVER_URL', '').rstrip('/')
    if not url:
        raise RuntimeError('COLLABORA_SERVER_URL is not configured in settings.')
    return url


def _collabora_public():
    """
    Public base URL the browser loads the editor from. With Collabora
    mounted at root paths behind nginx this is simply the site URL.
    """
    url = getattr(settings, 'COLLABORA_PUBLIC_URL', '').rstrip('/')
    return url or _collabora_internal()


def _wopi_src_for(request, file):
    """
    The absolute URL Collabora calls back to fetch/save the file.
    Must be reachable FROM THE COLLABORA CONTAINER — hence the internal
    docker-network host (http://web:8000), not the public domain.
    """
    host = getattr(settings, 'COLLABORA_WOPI_HOST', None)
    if host:
        base = host.rstrip('/')
    else:
        base = request.build_absolute_uri('/')[:-1]
    return f'{base}/files/collabora/wopi/files/{file.pk}'


# ── Discovery ────────────────────────────────────────────────────────────────

def _discovery_map():
    """
    Fetch and cache Collabora's WOPI discovery document, returning
    {ext: urlsrc}. The urlsrc contains the version-hashed editor path
    (/browser/<hash>/cool.html) which CHANGES on every image upgrade —
    this is why the path must never be hardcoded.
    """
    cached = cache.get(_DISCOVERY_CACHE_KEY)
    if cached:
        return cached

    url = f'{_collabora_internal()}/hosting/discovery'
    try:
        with urlopen(url, timeout=5) as resp:
            root = ET.fromstring(resp.read())
    except Exception as exc:
        logger.warning('Collabora discovery fetch failed (%s): %s', url, exc)
        return {}

    mapping = {}
    for action in root.iter('action'):
        ext = (action.get('ext') or '').lower()
        name = action.get('name') or ''
        urlsrc = action.get('urlsrc') or ''
        if not ext or not urlsrc:
            continue
        # Prefer the 'edit' action; fall back to 'view'.
        if name == 'edit' or ext not in mapping:
            mapping[ext] = urlsrc

    if mapping:
        cache.set(_DISCOVERY_CACHE_KEY, mapping, _DISCOVERY_CACHE_TTL)
    return mapping


def _editor_base_url(extension):
    """
    Resolve the public cool.html URL for a file extension.

    Discovery returns the INTERNAL host (or the server_name), so we
    rewrite scheme+host to COLLABORA_PUBLIC_URL and strip the WOPI
    optional-parameter placeholders (<ui=UI_LLCC&> style blocks).
    """
    urlsrc = _discovery_map().get(extension)

    if not urlsrc:
        # Last-resort fallback; logged loudly because it will break on
        # image upgrades if discovery keeps failing.
        logger.error(
            'No discovery urlsrc for .%s — falling back to conventional path. '
            'Check that the Collabora container is up and reachable at %s.',
            extension, _collabora_internal(),
        )
        urlsrc = f'{_collabora_public()}/browser/dist/cool.html?'

    # Strip <placeholder> option blocks from the discovery urlsrc.
    urlsrc = re.sub(r'<[^>]*>', '', urlsrc)

    # Rewrite to the public origin (browser-reachable, via nginx).
    pub = urlsplit(_collabora_public())
    parts = urlsplit(urlsrc)
    urlsrc = urlunsplit((pub.scheme, pub.netloc, parts.path, parts.query, ''))

    # Normalise so we can append our own query params.
    if '?' not in urlsrc:
        urlsrc += '?'
    elif not urlsrc.endswith(('?', '&')):
        urlsrc += '&'
    return urlsrc


def _editor_url(request, file, lang='en'):
    """Full cool.html URL (access_token is POSTed by the launch form)."""
    params = {
        'WOPISrc': _wopi_src_for(request, file),
        'lang':    lang,
        # 'closebutton': 'true',   # show Collabora's own close button
        # 'revisionhistory': 'true',
    }
    return f'{_editor_base_url(file.extension)}{urlencode(params)}'


# ── Editor launch ────────────────────────────────────────────────────────────

class CollaboraEditorView(LoginRequiredMixin, View):
    """
    GET /files/collabora/edit/<uuid>/

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

        # WOPI's `access_token_ttl` is NOT a duration: it is the ABSOLUTE
        # Unix timestamp in MILLISECONDS at which the token expires.
        # Passing the TTL itself (e.g. 36_000_000 ms) tells Collabora the
        # token expired on 1 Jan 1970 ~10:00, so it shows "Your session
        # has expired" the moment the editor loads — every single time.
        import time
        ttl_seconds = getattr(settings, 'COLLABORA_TOKEN_TTL_SECONDS', 36000)
        token_expiry_ms = int((time.time() + ttl_seconds) * 1000)

        ctx = {
            'file':         file,
            'session':      session,
            'editor_url':   editor_url,
            'access_token': token,
            'token_ttl_ms': token_expiry_ms,
            'permission':   permission,
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

    csrf_exempt is acceptable here because the endpoint only refreshes a
    timestamp on a session owned by request.user — a forged request can't
    read or modify anything else.
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
        except (CollaboraSession.DoesNotExist, ValueError):
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

    Called via navigator.sendBeacon on beforeunload (sendBeacon cannot
    set an X-CSRFToken header, hence csrf_exempt — same low-risk
    reasoning as the ping endpoint).
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
        except (CollaboraSession.DoesNotExist, ValueError):
            return JsonResponse({'ok': True})  # idempotent

        session.end()
        _broadcast_presence(session.file_id)
        return JsonResponse({'ok': True})


class PresenceListView(LoginRequiredMixin, View):
    """
    GET /files/collabora/files/<uuid>/presence/

    Returns JSON { "editors": [{ "user_id", "name", "initials",
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