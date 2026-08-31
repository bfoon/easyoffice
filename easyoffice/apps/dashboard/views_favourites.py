"""
apps/dashboard/views_favourites.py
==================================

JSON endpoints behind the Favourites tab.

    GET    /dashboard/favourites/            list mine
    POST   /dashboard/favourites/add/        pin one
    POST   /dashboard/favourites/remove/     unpin by url or id
    POST   /dashboard/favourites/reorder/    save a new order

URL wiring — in apps/dashboard/urls.py::

    from apps.dashboard import views_favourites as fav
    path('favourites/',         fav.FavouriteListView.as_view(),    name='favourites'),
    path('favourites/add/',     fav.FavouriteAddView.as_view(),     name='favourite_add'),
    path('favourites/remove/',  fav.FavouriteRemoveView.as_view(),  name='favourite_remove'),
    path('favourites/reorder/', fav.FavouriteReorderView.as_view(), name='favourite_reorder'),

SECURITY NOTE
-------------
``url`` is stored as given and later rendered into an href. It is
therefore validated on the way IN, not on the way out: site-relative
paths only. Without that check a favourite is a self-XSS primitive
(``javascript:…``) and an open-redirect surface (``//evil.example``)
that the user would happily click, because it is sitting on their own
dashboard looking like something they pinned.
"""

from __future__ import annotations

import json
import logging
import re

from django.contrib.auth.mixins import LoginRequiredMixin
from django.db import IntegrityError, transaction
from django.http import JsonResponse
from django.views.generic import View

from apps.dashboard.models import Favourite

logger = logging.getLogger(__name__)

MAX_FAVOURITES = 40

# Bootstrap Icons class names only — anything else becomes the default.
_ICON_RE = re.compile(r'^bi-[a-z0-9-]{1,50}$')
_COLOUR_RE = re.compile(r'^#[0-9a-fA-F]{6}$')


def _clean_url(raw):
    """
    Return a safe site-relative path, or None.

    Accepts   /tasks/ , /tasks/?view=team , /tasks/#section
    Rejects   javascript:… , data:… , https://elsewhere , //evil.example
    """
    url = (raw or '').strip()
    if not url or len(url) > 500:
        return None
    # Strip control characters first: "java\tscript:" defeats a naive
    # prefix check but is still executed by browsers.
    url = re.sub(r'[\x00-\x20]', '', url)
    if not url.startswith('/'):
        return None
    if url.startswith('//'):          # protocol-relative → off-site
        return None
    return url


def _clean_icon(raw):
    icon = (raw or '').strip().lower()
    return icon if _ICON_RE.match(icon) else 'bi-star'


def _clean_colour(raw):
    colour = (raw or '').strip()
    return colour if _COLOUR_RE.match(colour) else ''


def _serialize(fav):
    return {
        'id': str(fav.pk),
        'label': fav.label,
        'url': fav.url,
        'icon': fav.icon,
        'colour': fav.colour,
        'kind': fav.kind,
        'sort_order': fav.sort_order,
    }


def _body(request):
    """Accept either form-encoded or JSON, so callers can use either."""
    if request.content_type and 'application/json' in request.content_type:
        try:
            return json.loads(request.body.decode('utf-8') or '{}')
        except Exception:
            return {}
    return request.POST


class FavouriteListView(LoginRequiredMixin, View):
    def get(self, request):
        rows = Favourite.objects.filter(user=request.user)
        return JsonResponse({
            'ok': True,
            'favourites': [_serialize(f) for f in rows],
        })


class FavouriteAddView(LoginRequiredMixin, View):
    """
    POST label, url, icon?, colour?, kind?

    Idempotent: pinning something already pinned returns the existing
    tile rather than erroring. The star in the sidebar is a toggle, and a
    double-click must not produce a duplicate or a red error.
    """

    def post(self, request):
        data = _body(request)

        url = _clean_url(data.get('url'))
        if url is None:
            return JsonResponse(
                {'ok': False, 'error': 'That link cannot be pinned.'},
                status=400)

        label = ' '.join((data.get('label') or '').split())[:80]
        if not label:
            label = url.strip('/').split('/')[-1].replace('-', ' ').title() or 'Link'

        existing = Favourite.objects.filter(user=request.user, url=url).first()
        if existing:
            return JsonResponse({'ok': True, 'favourite': _serialize(existing),
                                 'created': False})

        if Favourite.objects.filter(user=request.user).count() >= MAX_FAVOURITES:
            return JsonResponse(
                {'ok': False,
                 'error': f'You can pin up to {MAX_FAVOURITES} items. '
                          f'Remove one first.'},
                status=400)

        last = (Favourite.objects.filter(user=request.user)
                .order_by('-sort_order').first())
        try:
            fav = Favourite.objects.create(
                user=request.user,
                label=label,
                url=url,
                icon=_clean_icon(data.get('icon')),
                colour=_clean_colour(data.get('colour')),
                kind=(data.get('kind') if data.get('kind') in
                      dict(Favourite.Kind.choices) else Favourite.Kind.NAV),
                sort_order=(last.sort_order + 1) if last else 0,
            )
        except IntegrityError:
            # Two tabs pinned the same thing at once — the unique
            # constraint did its job; return the winner.
            fav = Favourite.objects.filter(user=request.user, url=url).first()
            if fav is None:
                return JsonResponse(
                    {'ok': False, 'error': 'Could not pin that.'}, status=400)
            return JsonResponse({'ok': True, 'favourite': _serialize(fav),
                                 'created': False})

        return JsonResponse({'ok': True, 'favourite': _serialize(fav),
                             'created': True})


class FavouriteRemoveView(LoginRequiredMixin, View):
    """POST id=<uuid>  or  url=/tasks/ — whichever the caller has."""

    def post(self, request):
        data = _body(request)
        qs = Favourite.objects.filter(user=request.user)

        fav_id = (data.get('id') or '').strip()
        url = _clean_url(data.get('url'))

        if fav_id:
            qs = qs.filter(pk=fav_id)
        elif url:
            qs = qs.filter(url=url)
        else:
            return JsonResponse({'ok': False, 'error': 'Nothing to remove.'},
                                status=400)

        try:
            deleted, _ = qs.delete()
        except (ValueError, TypeError):
            deleted = 0
        return JsonResponse({'ok': True, 'removed': deleted})


class FavouriteReorderView(LoginRequiredMixin, View):
    """
    POST order=<comma-separated ids, in the new order>

    Only ids belonging to this user are touched, so a crafted list can
    reorder nothing but its author's own tiles.
    """

    def post(self, request):
        data = _body(request)
        raw = data.get('order')
        if isinstance(raw, str):
            ids = [i.strip() for i in raw.split(',') if i.strip()]
        elif isinstance(raw, (list, tuple)):
            ids = [str(i).strip() for i in raw if str(i).strip()]
        else:
            ids = []

        if not ids:
            return JsonResponse({'ok': False, 'error': 'No order given.'},
                                status=400)

        mine = {str(f.pk): f for f in
                Favourite.objects.filter(user=request.user)}

        with transaction.atomic():
            position = 0
            for fav_id in ids:
                fav = mine.pop(fav_id, None)
                if fav is None:
                    continue
                if fav.sort_order != position:
                    fav.sort_order = position
                    fav.save(update_fields=['sort_order'])
                position += 1
            # Anything the client didn't mention keeps a stable place at
            # the end rather than all collapsing to 0.
            for fav in mine.values():
                if fav.sort_order != position:
                    fav.sort_order = position
                    fav.save(update_fields=['sort_order'])
                position += 1

        return JsonResponse({'ok': True})
