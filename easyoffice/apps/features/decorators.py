"""
Helpers for code paths the URL gate cannot see — Celery tasks, signals,
dashboard widgets, management commands.

    from apps.features.decorators import feature_required, skip_if_disabled

    class MyView(feature_required('pos'), View): ...

    @skip_if_disabled('investments')
    def run_distributions(): ...
"""
from functools import wraps

from django.http import Http404


def feature_required(key):
    """Mixin factory for class-based views."""
    class _FeatureRequiredMixin:
        def dispatch(self, request, *args, **kwargs):
            from apps.features.services import is_enabled
            if not is_enabled(key, getattr(request, 'user', None)):
                raise Http404('This module is not available.')
            return super().dispatch(request, *args, **kwargs)
    _FeatureRequiredMixin.__name__ = f'FeatureRequired_{key}'
    return _FeatureRequiredMixin


def skip_if_disabled(key, default=None):
    """Decorator for background jobs: no-op while the feature is globally off."""
    def deco(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            from apps.features.services import mode_for
            if mode_for(key) == 'off':
                return default
            return fn(*args, **kwargs)
        return wrapper
    return deco
