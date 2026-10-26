"""
apps/features/middleware.py
───────────────────────────
FeatureGateMiddleware — blocks requests into a module that is switched off.

    • Browser requests get a friendly "module unavailable" page (HTTP 404,
      so a disabled module is indistinguishable from a missing one).
    • /api/ requests and anything asking for JSON get {"detail": ...} 404.
    • Superusers with "preview" enabled on that flag pass through; the page
      gets request.feature_preview = spec so base.html can show a banner.

Register AFTER AuthenticationMiddleware (needs request.user). Fails OPEN on
unexpected errors — a bug in the gate must never take the intranet down.
"""
import logging

from django.http import JsonResponse
from django.shortcuts import render

logger = logging.getLogger(__name__)


class FeatureGateMiddleware:
    ALWAYS_ALLOW = ('/static/', '/media/', '/favicon', '/admin/', '/features/')

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.feature_preview = None
        path = request.path or '/'
        if not path.startswith(self.ALWAYS_ALLOW):
            try:
                blocked = self._check(request, path)
            except Exception:
                logger.exception('FeatureGateMiddleware failed open for %s', path)
                blocked = None
            if blocked is not None:
                return blocked
        return self.get_response(request)

    def _check(self, request, path):
        from apps.features.registry import registry
        from apps.features.services import access_for, row_for

        spec = registry.resolve_path(path)
        if spec is None or spec.locked:
            return None
        if any(path.startswith(p) for p in spec.always_open_prefixes):
            return None

        decision = access_for(getattr(request, 'user', None), spec)
        if decision == 'allow':
            return None
        if decision == 'preview':
            request.feature_preview = spec
            return None

        message = (row_for(spec.key) or {}).get('off_message') or \
            f'{spec.name} is currently unavailable.'
        wants_json = (
            path.startswith('/api/')
            or 'application/json' in request.META.get('HTTP_ACCEPT', '')
            or request.headers.get('x-requested-with') == 'XMLHttpRequest'
        )
        if wants_json:
            return JsonResponse({'detail': message, 'feature': spec.key}, status=404)
        template = 'features/blocked.html'
        if any(path.startswith(p) for p in spec.standalone_prefixes):
            template = 'features/blocked_standalone.html'
        return render(request, template, {'spec': spec, 'message': message}, status=404)
