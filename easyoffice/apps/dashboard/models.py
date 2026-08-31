"""
apps/dashboard/models.py
========================

Favourites — the links a person has pinned for themselves.

WHY THERE IS NO APP REGISTRY HERE
---------------------------------
The obvious way to build an Odoo-style apps grid is a hard-coded
catalogue: a list of every module with its label, icon, URL and the
permission needed to see it. That list then has to be kept in step with
base.html by hand, forever. Miss an entry and a module is invisible;
change a permission in one place and not the other and people see tiles
that 403 when clicked.

So there is no catalogue. A favourite stores the label, URL and icon it
was created from, and the picker reads those straight out of the
sidebar navigation that is already rendered on the page. The sidebar has
already applied every permission check — a link a user cannot see is not
in their DOM, so it cannot be pinned. The catalogue and the nav can
never drift apart because there is only one of them.

The trade-off is that a favourite is a SNAPSHOT. Rename a module and old
favourites keep the old label until they are re-pinned. That is worth
it: the alternative is a second source of truth, and this one degrades
into "slightly stale text" rather than "silently wrong permissions".
"""

import uuid

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _


class Favourite(models.Model):
    """One pinned link on a person's Favourites tab."""

    class Kind(models.TextChoices):
        NAV  = 'nav',  _('Menu item')
        PAGE = 'page', _('Saved page')

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name='favourites')

    label = models.CharField(max_length=80)
    url = models.CharField(
        max_length=500,
        help_text='Site-relative path, e.g. /tasks/ — never an absolute URL.')
    icon = models.CharField(
        max_length=60, default='bi-star',
        help_text='Bootstrap Icons class name.')
    colour = models.CharField(
        max_length=7, blank=True,
        help_text='Optional tile accent, #rrggbb. Blank uses a colour '
                  'derived from the label so tiles are still tellable apart.')
    kind = models.CharField(max_length=8, choices=Kind.choices,
                            default=Kind.NAV)

    # Lower sorts first. Reordering rewrites this for the whole set, which
    # is fine — nobody has hundreds of favourites.
    sort_order = models.PositiveSmallIntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['sort_order', 'created_at']
        # One pin per destination. Pinning something already pinned is a
        # no-op rather than a duplicate tile.
        unique_together = [('user', 'url')]
        indexes = [
            models.Index(fields=['user', 'sort_order']),
        ]

    def __str__(self):
        return f'{self.user} · {self.label}'
