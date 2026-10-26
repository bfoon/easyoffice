"""
apps/features/registry.py
─────────────────────────
The module catalogue. Every switchable part of EasyOffice is described by a
FeatureSpec. Specs are code (versioned with the app); the on/off STATE lives
in the database (FeatureFlag) so a superuser can flip it at runtime.

How an app plugs in
───────────────────
Create `apps/<your_app>/feature_manifest.py`:

    from apps.features.registry import FeatureSpec, register

    register(FeatureSpec(
        key='investments',
        name='Investments',
        description='Investor onboarding, wallets, listings and profit sharing.',
        icon='bi-graph-up-arrow',
        category='Finance',
        url_prefixes=('/investments/', '/invest/'),
        requires=('inventory',),
    ))

FeaturesConfig.ready() autodiscovers every `feature_manifest` module, so no
central edit is needed. Path → feature resolution uses the LONGEST matching
prefix, which lets a sub-feature ('/finance/sales-targets/') be switched
independently of its parent ('/finance/').
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple


@dataclass(frozen=True)
class FeatureSpec:
    key: str
    name: str
    description: str = ''
    icon: str = 'bi-puzzle'
    category: str = 'General'
    url_prefixes: Tuple[str, ...] = ()
    requires: Tuple[str, ...] = ()
    # Locked features can never be switched off (auth, dashboard, the
    # control panel itself). Prevents a superuser locking everyone out.
    locked: bool = False
    default_enabled: bool = True
    # Prefixes that stay reachable even when the feature is OFF — e.g.
    # public token links you don't want to break mid-flow.
    always_open_prefixes: Tuple[str, ...] = ()
    # Paths that face outsiders (portals). While the feature is off they get
    # a plain page without the staff sidebar.
    standalone_prefixes: Tuple[str, ...] = ()
    order: int = 100


class _Registry:
    def __init__(self):
        self._specs: Dict[str, FeatureSpec] = {}
        self._prefix_index: Optional[List[Tuple[str, str]]] = None

    # ── registration ────────────────────────────────────────────────────
    def register(self, spec: FeatureSpec) -> FeatureSpec:
        if not spec.key or not spec.key.replace('_', '').isalnum():
            raise ValueError(f'Invalid feature key: {spec.key!r}')
        self._specs[spec.key] = spec          # later registration wins
        self._prefix_index = None
        return spec

    def unregister(self, key: str):
        self._specs.pop(key, None)
        self._prefix_index = None

    # ── lookup ──────────────────────────────────────────────────────────
    def get(self, key: str) -> Optional[FeatureSpec]:
        return self._specs.get(key)

    def all(self) -> List[FeatureSpec]:
        return sorted(self._specs.values(), key=lambda s: (s.category, s.order, s.name))

    def keys(self) -> Iterable[str]:
        return self._specs.keys()

    def by_category(self) -> List[Tuple[str, List[FeatureSpec]]]:
        groups: Dict[str, List[FeatureSpec]] = {}
        for spec in self.all():
            groups.setdefault(spec.category, []).append(spec)
        return sorted(groups.items(), key=lambda kv: kv[0])

    def dependents_of(self, key: str) -> List[FeatureSpec]:
        """Features that directly require `key`."""
        return [s for s in self._specs.values() if key in s.requires]

    def all_dependents_of(self, key: str) -> List[FeatureSpec]:
        """Transitive closure of dependents."""
        seen, stack, out = set(), [key], []
        while stack:
            k = stack.pop()
            for dep in self.dependents_of(k):
                if dep.key not in seen:
                    seen.add(dep.key)
                    out.append(dep)
                    stack.append(dep.key)
        return out

    def all_requirements_of(self, key: str) -> List[FeatureSpec]:
        seen, stack, out = set(), [key], []
        while stack:
            spec = self._specs.get(stack.pop())
            if not spec:
                continue
            for req in spec.requires:
                if req not in seen and req in self._specs:
                    seen.add(req)
                    out.append(self._specs[req])
                    stack.append(req)
        return out

    # ── path resolution ─────────────────────────────────────────────────
    def _index(self) -> List[Tuple[str, str]]:
        if self._prefix_index is None:
            pairs = []
            for spec in self._specs.values():
                for p in spec.url_prefixes:
                    pairs.append((p, spec.key))
            # Longest prefix first so sub-features beat their parent.
            pairs.sort(key=lambda kv: len(kv[0]), reverse=True)
            self._prefix_index = pairs
        return self._prefix_index

    def resolve_path(self, path: str) -> Optional[FeatureSpec]:
        for prefix, key in self._index():
            if path.startswith(prefix):
                return self._specs.get(key)
        return None


registry = _Registry()
register = registry.register
