"""
Period resolution — turns ?from=&to=&preset= GET params into a Period
object the views and JSON endpoint can both rely on.

The control_center.html filter bar uses these preset names exactly:
  mtd, qtd, ytd, last_30, last_90, last_year
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Mapping


@dataclass(frozen=True)
class Period:
    label: str
    start: date
    end: date

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1


PRESETS = {
    'mtd':       'Month to date',
    'qtd':       'Quarter to date',
    'ytd':       'Year to date',
    'last_30':   'Last 30 days',
    'last_90':   'Last 90 days',
    'last_year': 'Last year',
}


def resolve_period(params: Mapping[str, str], today: date | None = None) -> Period:
    today = today or date.today()
    f = (params.get('from') or '').strip()
    t = (params.get('to') or '').strip()
    preset = (params.get('preset') or '').strip().lower()

    if f and t:
        try:
            return Period(
                label=f'{f} to {t}',
                start=date.fromisoformat(f),
                end=date.fromisoformat(t),
            )
        except ValueError:
            pass  # fall through to preset

    if preset == 'mtd':
        return Period('Month to date', today.replace(day=1), today)
    if preset == 'qtd':
        q_start_month = ((today.month - 1) // 3) * 3 + 1
        return Period('Quarter to date', date(today.year, q_start_month, 1), today)
    if preset == 'last_30':
        return Period('Last 30 days', today - timedelta(days=29), today)
    if preset == 'last_90':
        return Period('Last 90 days', today - timedelta(days=89), today)
    if preset == 'last_year':
        return Period(
            'Last year',
            date(today.year - 1, 1, 1),
            date(today.year - 1, 12, 31),
        )

    # Default: year to date
    return Period('Year to date', date(today.year, 1, 1), today)


def prior_period(p: Period) -> Period:
    """The period immediately before `p`, of equal length."""
    length = p.days
    end = p.start - timedelta(days=1)
    start = end - timedelta(days=length - 1)
    return Period(f'Prior {length}d', start, end)
