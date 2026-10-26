"""
apps/features/services.py
─────────────────────────
Single source of truth for "is this module on for this person?".

Public API
    is_enabled(key, user=None) -> bool
    access_for(user, spec)     -> 'allow' | 'preview' | 'deny'
    feature_map(user)          -> FeatureMap   (templates: {{ features.pos }})
    set_mode(key, mode, actor=..., cascade=False) -> dict
    invalidate_cache()

State is cached in the Django cache for CACHE_TTL seconds and busted on
every FeatureFlag change (see signals.py). If the cache backend is down the
code falls back to the database, so Redis trouble can never 500 a page.
"""
from __future__ import annotations

import logging
from typing import Dict, Optional

from django.core.cache import cache
from django.db import transaction

from apps.features.registry import registry

logger = logging.getLogger(__name__)

CACHE_KEY = 'eo:features:state:v1'
CACHE_TTL = 60

ON, PILOT, OFF = 'on', 'pilot', 'off'
VALID_MODES = (ON, PILOT, OFF)


class FeatureDependencyError(Exception):
    def __init__(self, message, blocking=None):
        super().__init__(message)
        self.blocking = blocking or []


# ─────────────────────────────────────────────────────────────────────────────
# State loading
# ─────────────────────────────────────────────────────────────────────────────

def _load_from_db() -> Dict[str, dict]:
    from apps.features.models import FeatureFlag
    state = {}
    try:
        rows = FeatureFlag.objects.prefetch_related('pilot_users', 'pilot_groups')
        for f in rows:
            state[f.key] = {
                'mode': f.mode,
                'pilot_user_ids': {str(u.pk) for u in f.pilot_users.all()},
                'pilot_group_ids': {g.pk for g in f.pilot_groups.all()},
                'superuser_preview': f.superuser_preview,
                'off_message': f.off_message,
            }
    except Exception:
        # Table missing (before migrate) or DB hiccup → everything defaults.
        logger.warning('features: could not load flags, using defaults', exc_info=True)
    return state


def _state() -> Dict[str, dict]:
    try:
        cached = cache.get(CACHE_KEY)
    except Exception:
        cached = None
    if cached is not None:
        return cached
    state = _load_from_db()
    try:
        cache.set(CACHE_KEY, state, CACHE_TTL)
    except Exception:
        pass
    return state


def invalidate_cache():
    try:
        cache.delete(CACHE_KEY)
    except Exception:
        pass


def row_for(key: str) -> Optional[dict]:
    return _state().get(key)


def mode_for(key: str) -> str:
    spec = registry.get(key)
    if spec is None or spec.locked:
        return ON
    row = row_for(key)
    if row:
        return row['mode']
    return ON if spec.default_enabled else OFF


# ─────────────────────────────────────────────────────────────────────────────
# Per-user evaluation
# ─────────────────────────────────────────────────────────────────────────────

def _group_ids(user) -> set:
    cached = getattr(user, '_feature_group_ids', None)
    if cached is None:
        try:
            cached = set(user.groups.values_list('pk', flat=True))
        except Exception:
            cached = set()
        try:
            user._feature_group_ids = cached
        except Exception:
            pass
    return cached


def _in_pilot(key: str, user) -> bool:
    if user is None or not getattr(user, 'is_authenticated', False):
        return False
    if user.is_superuser:
        return True
    row = row_for(key) or {}
    if str(user.pk) in row.get('pilot_user_ids', set()):
        return True
    return bool(_group_ids(user) & row.get('pilot_group_ids', set()))


def _requirements_met(key: str, user, _seen=None) -> bool:
    """A feature is only usable if everything it requires is usable too."""
    _seen = _seen or set()
    spec = registry.get(key)
    if not spec:
        return True
    for req in spec.requires:
        if req in _seen:
            continue
        _seen.add(req)
        if not _enabled_self(req, user) or not _requirements_met(req, user, _seen):
            return False
    return True


def _enabled_self(key: str, user) -> bool:
    mode = mode_for(key)
    if mode == ON:
        return True
    if mode == PILOT:
        return _in_pilot(key, user)
    return False


def is_enabled(key: str, user=None) -> bool:
    """True if `key` is usable by `user` (or globally on, when user is None).
    Unknown keys are treated as enabled so a typo never hides a module."""
    if registry.get(key) is None:
        return True
    return _enabled_self(key, user) and _requirements_met(key, user)


def access_for(user, spec) -> str:
    if spec is None or spec.locked:
        return 'allow'
    if is_enabled(spec.key, user):
        return 'allow'
    row = row_for(spec.key) or {}
    if (user is not None and getattr(user, 'is_superuser', False)
            and row.get('superuser_preview')):
        return 'preview'
    return 'deny'


class FeatureMap(dict):
    """dict for templates; unknown keys resolve to True."""

    def __missing__(self, key):
        return True


def feature_map(user) -> FeatureMap:
    fm = FeatureMap()
    for spec in registry.all():
        fm[spec.key] = is_enabled(spec.key, user)
    return fm


# ─────────────────────────────────────────────────────────────────────────────
# Mutations
# ─────────────────────────────────────────────────────────────────────────────

def _log(key, old, new, actor, ip, note):
    from apps.features.models import FeatureAuditLog
    FeatureAuditLog.objects.create(
        key=key, old_mode=old or '', new_mode=new or '',
        actor=actor if getattr(actor, 'pk', None) else None,
        ip_address=ip, note=(note or '')[:400],
    )


def set_mode(key: str, mode: str, *, actor=None, cascade: bool = False,
             ip: Optional[str] = None, note: str = '') -> dict:
    """
    Change a feature's mode.

    Turning ON / PILOT also switches ON any required feature that is off
    (reported in result['auto_enabled']).

    Turning OFF a feature that others depend on raises
    FeatureDependencyError unless cascade=True, in which case dependents are
    switched off too (result['cascaded']).
    """
    from apps.features.models import FeatureFlag

    spec = registry.get(key)
    if spec is None:
        raise ValueError(f'Unknown feature "{key}".')
    if spec.locked:
        raise ValueError(f'"{spec.name}" is part of the core platform and cannot be changed.')
    if mode not in VALID_MODES:
        raise ValueError(f'Invalid mode "{mode}".')

    result = {'changed': [], 'auto_enabled': [], 'cascaded': []}

    with transaction.atomic():
        if mode == OFF:
            live_dependents = [d for d in registry.all_dependents_of(key)
                               if mode_for(d.key) != OFF and not d.locked]
            if live_dependents and not cascade:
                names = ', '.join(d.name for d in live_dependents)
                raise FeatureDependencyError(
                    f'{names} depend{"s" if len(live_dependents) == 1 else ""} on '
                    f'{spec.name}. Turn {"it" if len(live_dependents) == 1 else "them"} '
                    f'off as well, or keep {spec.name} on.',
                    blocking=live_dependents,
                )
            for dep in live_dependents:
                _apply(FeatureFlag, dep.key, OFF, actor, ip,
                       f'Cascaded from {spec.name}')
                result['cascaded'].append(dep)
        else:
            for req in registry.all_requirements_of(key):
                if not req.locked and mode_for(req.key) == OFF:
                    _apply(FeatureFlag, req.key, ON, actor, ip,
                           f'Required by {spec.name}')
                    result['auto_enabled'].append(req)

        if _apply(FeatureFlag, key, mode, actor, ip, note):
            result['changed'].append(spec)

    invalidate_cache()
    return result


def _apply(FeatureFlag, key, mode, actor, ip, note) -> bool:
    old = mode_for(key)
    flag, _ = FeatureFlag.objects.select_for_update().get_or_create(key=key)
    if flag.mode == mode and old == mode:
        return False
    flag.mode = mode
    flag.updated_by = actor if getattr(actor, 'pk', None) else None
    flag.save()
    _log(key, old, mode, actor, ip, note)
    invalidate_cache()
    return True


def client_ip(request) -> Optional[str]:
    fwd = request.META.get('HTTP_X_FORWARDED_FOR', '')
    if fwd:
        return fwd.split(',')[0].strip() or None
    return request.META.get('REMOTE_ADDR') or None
